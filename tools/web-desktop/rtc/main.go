package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"embed"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/rtp/codecs"
	"github.com/pion/webrtc/v4"
)

//go:embed ui/*
var files embed.FS

const frameBytes = 1920 * 1200 * 4
const audioChunk = 3840 // stereo S16LE, 48 kHz, 20 ms

type gateway struct {
	host, target, port, cert, source, encoder string
	origin                                    string
	lan                                       netip.Prefix
	attempts                                  *attemptLimiter
	localAttempts                             *attemptLimiter
	api                                       *webrtc.API
	slots                                     chan struct{}
	sessions                                  sessionList
	leases                                    desktopLeases
	startup                                   startupGate
	apple                                     *appleFlow
}
type offer struct {
	Username string `json:"username"`
	Password string `json:"password"`
	SDP      string `json:"sdp"`
	Managed  bool   `json:"managed,omitempty"`
	Grant    string `json:"grant,omitempty"`
	Resume   string `json:"resume,omitempty"`
}

var videoCodec = webrtc.RTPCodecCapability{
	MimeType: webrtc.MimeTypeH264, ClockRate: 90000,
	// 1920×1200 at 30 fps needs level 5 (9,000 macroblocks per frame).
	SDPFmtpLine:  "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e032",
	RTCPFeedback: []webrtc.RTCPFeedback{{Type: "nack"}, {Type: "nack", Parameter: "pli"}},
}
var audioCodec = webrtc.RTPCodecCapability{
	MimeType: webrtc.MimeTypeOpus, ClockRate: 48000, Channels: 2,
	SDPFmtpLine: "minptime=10;useinbandfec=1",
}

func newAPI(address netip.Addr, udp net.PacketConn) (*webrtc.API, error) {
	var codecs webrtc.MediaEngine
	if err := codecs.RegisterCodec(webrtc.RTPCodecParameters{RTPCodecCapability: videoCodec, PayloadType: 102}, webrtc.RTPCodecTypeVideo); err != nil {
		return nil, err
	}
	if err := codecs.RegisterCodec(webrtc.RTPCodecParameters{RTPCodecCapability: audioCodec, PayloadType: 111}, webrtc.RTPCodecTypeAudio); err != nil {
		return nil, err
	}
	var settings webrtc.SettingEngine
	settings.SetIncludeLoopbackCandidate(address.IsLoopback())
	settings.SetIPFilter(func(ip net.IP) bool { return ip.Equal(net.IP(address.AsSlice())) })
	settings.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4})
	if udp != nil {
		settings.SetICEUDPMux(webrtc.NewICEUDPMux(nil, udp))
	}
	return webrtc.NewAPI(webrtc.WithMediaEngine(&codecs), webrtc.WithSettingEngine(settings)), nil
}

func (g *gateway) serve(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; form-action 'none'")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
	if !strings.EqualFold(r.Host, g.host) || (g.lan.IsValid() && (r.TLS == nil || !g.lan.Contains(peerIP(r.RemoteAddr)))) {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}
	if g.apple != nil && strings.HasPrefix(r.URL.Path, "/apple/") {
		g.apple.route(w, r, g.origin, func() bool { return g.attempts.allow(peerIP(r.RemoteAddr), time.Now()) })
		return
	}
	if r.Method == http.MethodGet {
		switch r.URL.Path {
		case "/":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			data, _ := files.ReadFile("ui/index.html")
			_, _ = w.Write(data)
			return
		case "/desktop.js", "/audio-lab.js":
			w.Header().Set("Content-Type", "text/javascript; charset=utf-8")
			data, _ := files.ReadFile("ui" + r.URL.Path)
			_, _ = w.Write(data)
			return
		}
	}
	if r.Method == http.MethodPost && r.URL.Path == "/session" {
		g.session(w, r)
		return
	}
	if r.Method == http.MethodPost && r.URL.Path == "/session/end" {
		g.endDesktop(w, r)
		return
	}
	http.NotFound(w, r)
}

func (g *gateway) session(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != g.origin || r.Header.Get("Content-Type") != "application/json" {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}
	var req offer
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 48*1024))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&req) != nil || decoder.Decode(new(any)) != io.EOF || len(req.SDP) < 20 || !g.validOffer(req) {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}
	attempts := g.localAttempts
	if req.Managed {
		attempts = g.attempts
	}
	if req.Resume == "" && attempts != nil && !attempts.allow(peerIP(r.RemoteAddr), time.Now()) {
		w.Header().Set("Retry-After", "60")
		http.Error(w, "Too many connection attempts", http.StatusTooManyRequests)
		return
	}
	transport, lease, old, credentials, status := g.attach(w, r, req)
	if status != 0 {
		http.Error(w, http.StatusText(status), status)
		return
	}
	if !g.handoff(lease, transport, old, r.Context()) {
		transport.cancel()
		g.releaseTransport(lease, transport)
		return
	}
	pc, err := g.api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		transport.cancel()
		g.releaseTransport(lease, transport)
		http.Error(w, "Peer initialization failed", http.StatusInternalServerError)
		return
	}
	ctx, cancel := transport.ctx, transport.cancel
	loginFields := map[string]string{"username": credentials.Username, "password": credentials.Password}
	if req.Managed {
		loginFields["screen_key_sha256"] = credentials.ScreenKey
	}
	login, _ := json.Marshal(loginFields)
	delete(loginFields, "password")
	req.Password = ""
	username := credentials.Username
	credentials = managedReply{}
	var loginLock sync.Mutex
	wipeLogin := func() {
		loginLock.Lock()
		defer loginLock.Unlock()
		clear(login)
		login = nil
	}
	defer func() {
		if err != nil {
			cancel()
		}
	}()
	// A replacement must wait for both the peer and its desktop pipeline.
	// Claiming pipelineDone here prevents an arriving connected callback
	// from starting it after cleanup has already taken ownership.
	var started atomic.Bool
	var interactive atomic.Bool
	var desktopPhase atomic.Uint32
	pipelineDone := make(chan struct{})
	go func() {
		<-ctx.Done()
		wipeLogin()
		if started.CompareAndSwap(false, true) {
			close(pipelineDone)
		}
		_ = pc.Close()
		<-pipelineDone
		g.releaseTransport(lease, transport)
	}()
	if lease != nil {
		limit := time.AfterFunc(time.Until(lease.until), cancel)
		context.AfterFunc(ctx, func() { limit.Stop() })
	}

	addTrack := func(codec webrtc.RTPCodecCapability, id string) (*webrtc.TrackLocalStaticRTP, error) {
		track, err := webrtc.NewTrackLocalStaticRTP(codec, id, "desktop")
		if err != nil {
			return nil, err
		}
		sender, err := pc.AddTrack(track)
		if err != nil {
			return nil, err
		}
		go func() {
			buf := make([]byte, 2048)
			for {
				if _, _, err := sender.Read(buf); err != nil {
					return
				}
			}
		}()
		return track, nil
	}
	video, err := addTrack(videoCodec, "screen")
	if err != nil {
		http.Error(w, "Video setup failed", 500)
		return
	}
	audio, err := addTrack(audioCodec, "sound")
	if err != nil {
		http.Error(w, "Audio setup failed", 500)
		return
	}
	input := make(chan []byte, 48)
	var dataChannel atomic.Pointer[webrtc.DataChannel]
	pc.OnDataChannel(func(dc *webrtc.DataChannel) {
		if dc.Label() != "desktop-input" || !dataChannel.CompareAndSwap(nil, dc) {
			_ = dc.Close()
			return
		}
		dc.OnOpen(func() {
			switch desktopPhase.Load() {
			case 1:
				_ = dc.SendText("desktop-queued")
			case 2:
				_ = dc.SendText("desktop-starting")
			}
		})
		dc.OnClose(cancel)
		dc.OnMessage(func(msg webrtc.DataChannelMessage) {
			if !interactive.Load() || !msg.IsString || len(msg.Data) > 4095 ||
				bytes.ContainsAny(msg.Data, "\r\n") || !json.Valid(msg.Data) {
				return
			}
			select {
			case input <- msg.Data:
			case <-ctx.Done():
			default:
				// Fail closed rather than dropping a key or mouse release.
				cancel()
			}
		})
	})
	timeout := time.AfterFunc(18*time.Second, cancel)
	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		switch state {
		case webrtc.PeerConnectionStateConnected:
			if !started.CompareAndSwap(false, true) {
				return
			}
			timeout.Stop()
			loginLock.Lock()
			credentials := append([]byte(nil), login...)
			clear(login)
			login = nil
			loginLock.Unlock()
			go func() {
				defer close(pipelineDone)
				defer clear(credentials)
				if ctx.Err() != nil {
					clear(credentials)
					return
				}
				if req.Managed {
					defer g.apple.takeControl(username, cancel)()
				}
				var leave func()
				var releaseStartup func()
				var warmup *time.Timer
				defer func() {
					if warmup != nil {
						warmup.Stop()
					}
					if releaseStartup != nil {
						releaseStartup()
					}
					if leave != nil {
						leave()
					}
				}()
				report := func(message string) {
					switch message {
					case "desktop-queued":
						desktopPhase.Store(1)
					case "desktop-starting":
						desktopPhase.Store(2)
					}
					if message == "desktop-ready" {
						desktopPhase.Store(3)
						g.markDesktopReady(lease, transport)
						interactive.Store(true)
						if releaseStartup != nil && warmup == nil {
							warmup = time.AfterFunc(desktopWarmup, releaseStartup)
						}
						if leave == nil {
							leave = g.sessions.add(username)
						}
						return
					}
					if dc := dataChannel.Load(); dc != nil && dc.ReadyState() == webrtc.DataChannelStateOpen {
						_ = dc.SendText(message)
					}
				}
				// Both login paths can start Aqua, which needs exclusive startup
				// time. Existing sessions continue streaming while this one waits.
				report("desktop-queued")
				wait, stop := context.WithTimeout(ctx, startupQueueLimit)
				var err error
				releaseStartup, err = g.startup.acquire(wait)
				stop()
				if err != nil {
					if errors.Is(err, context.DeadlineExceeded) {
						report("desktop-queue-timeout")
					}
					cancel()
					return
				}
				report("desktop-starting")
				if err := g.streamDesktop(ctx, video, audio, credentials, input, report, req.Managed); err != nil && ctx.Err() == nil {
					log.Printf("Desktop pipeline stopped: %v", err)
					report("desktop-ended")
				}
				cancel()
			}()
		case webrtc.PeerConnectionStateDisconnected:
			// Let ICE recover short interruptions. Its Failed state still
			// ends the session and frees the slot on a sustained outage.
		case webrtc.PeerConnectionStateFailed, webrtc.PeerConnectionStateClosed:
			timeout.Stop()
			cancel()
		}
	})
	if err = pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: req.SDP}); err != nil {
		http.Error(w, "Invalid offer", 400)
		return
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		http.Error(w, "Could not create answer", 400)
		return
	}
	gathering := webrtc.GatheringCompletePromise(pc)
	if err = pc.SetLocalDescription(answer); err != nil {
		http.Error(w, "Could not apply answer", 500)
		return
	}
	select {
	case <-gathering:
	case <-ctx.Done():
		http.Error(w, "Connection expired", 504)
		return
	case <-r.Context().Done():
		cancel()
		return
	}
	w.Header().Set("Content-Type", "application/json")
	answerSDP := struct {
		*webrtc.SessionDescription
		Resume string `json:"resume,omitempty"`
	}{SessionDescription: pc.LocalDescription()}
	if lease != nil {
		answerSDP.Resume = lease.id
	}
	err = json.NewEncoder(w).Encode(answerSDP)
}

func (g *gateway) validOffer(req offer) bool {
	if req.Managed {
		return g.apple != nil && req.Username == "" && req.Password == "" &&
			((req.Grant == "" && appleSelector.MatchString(req.Resume)) ||
				(req.Resume == "" && appleSelector.MatchString(req.Grant)))
	}
	return req.Resume == "" && req.Grant == "" && len(req.Username) >= 1 && len(req.Username) <= 64 &&
		len(req.Password) >= 1 && len(req.Password) <= 1024
}

type mediaBuffer struct {
	frameMu  sync.Mutex
	frames   [3][]byte
	latest   *[]byte
	encoding *[]byte
	updates  atomic.Uint64
	mu       sync.Mutex
	audio    pcmTimeline
}

// One pipe reader writes into a slot outside the lock. Neither the last
// complete frame nor the frame pinned by the encoder writer can be reused.
func (b *mediaBuffer) frameSlot() *[]byte {
	b.frameMu.Lock()
	defer b.frameMu.Unlock()
	for i := range b.frames {
		if slot := &b.frames[i]; slot != b.latest && slot != b.encoding {
			return slot
		}
	}
	panic("no writable frame slot")
}

func (b *mediaBuffer) pinFrame() []byte {
	frame, _ := b.pinFrameAfter(0, true)
	return frame
}

func (b *mediaBuffer) pinFrameAfter(previous uint64, repeat bool) ([]byte, uint64) {
	b.frameMu.Lock()
	defer b.frameMu.Unlock()
	generation := b.updates.Load()
	if b.latest == nil || (!repeat && generation == previous) {
		return nil, generation
	}
	b.encoding = b.latest
	return *b.encoding, generation
}

func (b *mediaBuffer) unpinFrame() {
	b.frameMu.Lock()
	b.encoding = nil
	b.frameMu.Unlock()
}

func (b *mediaBuffer) read(reader io.Reader) error {
	var header [5]byte
	for {
		if _, err := io.ReadFull(reader, header[:]); err != nil {
			return err
		}
		n := int(binary.BigEndian.Uint32(header[1:]))
		switch header[0] {
		case 1:
			if n != frameBytes {
				return errors.New("invalid frame size")
			}
			slot := b.frameSlot()
			if *slot == nil {
				*slot = make([]byte, frameBytes)
			}
			if _, err := io.ReadFull(reader, *slot); err != nil {
				return err
			}
			b.frameMu.Lock()
			b.latest = slot
			b.updates.Add(1)
			b.frameMu.Unlock()
			continue
		case 2:
			if n < 8 || n > 19204 || n%4 != 0 {
				return errors.New("invalid PCM size")
			}
		default:
			return errors.New("unknown media packet")
		}
		data := make([]byte, n)
		if _, err := io.ReadFull(reader, data); err != nil {
			return err
		}
		b.mu.Lock()
		b.audio.insert(time.Now(), binary.BigEndian.Uint32(data[:4]), data[4:])
		b.mu.Unlock()
	}
}

func pump(source io.Reader, track *webrtc.TrackLocalStaticRTP, clock, unit uint32, payloader rtp.Payloader) error {
	return pumpPackets(source, track.WriteRTP, clock, unit, payloader)
}

func pumpPackets(source io.Reader, write func(*rtp.Packet) error, clock, unit uint32, payloader rtp.Payloader) error {
	packetizer := rtp.NewPacketizer(1200, 0, 0, payloader, rtp.NewRandomSequencer(), clock)
	var header [8]byte
	var next uint32
	for {
		if _, err := io.ReadFull(source, header[:]); err != nil {
			return err
		}
		n := binary.BigEndian.Uint32(header[:4])
		index := binary.BigEndian.Uint32(header[4:])
		if n == 0 || n > 4*1024*1024 {
			return errors.New("invalid encoded packet size")
		}
		// The encoder may drop frames under load. Preserve elapsed RTP time
		// for forward gaps, including wrap, and discard late/duplicate output.
		delta := int32(index - next)
		if delta < 0 {
			if _, err := io.CopyN(io.Discard, source, int64(n)); err != nil {
				return err
			}
			continue
		}
		frame := make([]byte, n)
		if _, err := io.ReadFull(source, frame); err != nil {
			return err
		}
		packetizer.SkipSamples(uint32(delta) * unit)
		for _, packet := range packetizer.Packetize(frame, unit) {
			if err := write(packet); err != nil {
				return err
			}
		}
		next = index + 1
	}
}

func (g *gateway) stream(parent context.Context, video, audio *webrtc.TrackLocalStaticRTP, login []byte, inputs <-chan []byte, report func(string)) error {
	return g.streamDesktop(parent, video, audio, login, inputs, report, false)
}

func (g *gateway) streamDesktop(parent context.Context, video, audio *webrtc.TrackLocalStaticRTP, login []byte, inputs <-chan []byte, report func(string), managed bool) error {
	defer clear(login)
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	source := exec.CommandContext(ctx, g.source, "--web-source", g.target, g.port, g.cert)
	if managed {
		source = exec.CommandContext(ctx, "/Library/Application Support/ZMacRDS/Runtime/bin/python3.13", "-I", "-B", "-u", "/usr/local/libexec/zmacrds-managed-screen.py")
	}
	in, err := source.StdinPipe()
	if err != nil {
		return err
	}
	out, err := source.StdoutPipe()
	if err != nil {
		return err
	}
	source.Stderr = os.Stderr
	if err := source.Start(); err != nil {
		return fmt.Errorf("start desktop source: %w", err)
	}
	defer func() { _ = in.Close(); cancel(); _ = source.Wait() }()
	if _, err := in.Write(append(login, '\n')); err != nil {
		return fmt.Errorf("initialize desktop source: %w", err)
	}
	clear(login)
	errorsOut := make(chan error, 8)
	buffer := &mediaBuffer{}
	go func() { errorsOut <- fmt.Errorf("desktop source output: %w", buffer.read(out)) }()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case event := <-inputs:
				if _, err := in.Write(append(event, '\n')); err != nil {
					errorsOut <- fmt.Errorf("desktop input pipe: %w", err)
					return
				}
			}
		}
	}()
	tick := time.NewTicker(20 * time.Millisecond)
	defer tick.Stop()
	firstFrameLimit := 100 * time.Second
	if managed {
		firstFrameLimit = 190 * time.Second
	}
	ready := time.NewTimer(firstFrameLimit)
	defer ready.Stop()
	for buffer.updates.Load() == 0 {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-errorsOut:
			return err
		case <-ready.C:
			return errors.New("no authenticated desktop frame")
		case <-tick.C:
		}
	}

	vr, vw, err := os.Pipe()
	if err != nil {
		return err
	}
	defer vr.Close()
	defer vw.Close()
	ar, aw, err := os.Pipe()
	if err != nil {
		return err
	}
	defer ar.Close()
	defer aw.Close()
	videoOutput, childVideo, err := os.Pipe()
	if err != nil {
		return err
	}
	defer videoOutput.Close()
	defer childVideo.Close()
	audioOutput, childAudio, err := os.Pipe()
	if err != nil {
		return err
	}
	defer audioOutput.Close()
	defer childAudio.Close()

	encoder := exec.CommandContext(ctx, g.encoder)
	encoder.ExtraFiles = []*os.File{vr, ar, childVideo, childAudio}
	encoder.Stderr = os.Stderr
	if err := encoder.Start(); err != nil {
		return fmt.Errorf("start media encoder: %w", err)
	}
	report("desktop-ready")
	_ = vr.Close()
	_ = ar.Close()
	_ = childVideo.Close()
	_ = childAudio.Close()
	encoderDone := make(chan struct{})
	defer func() { cancel(); <-encoderDone }()
	go func() {
		defer close(encoderDone)
		if err := encoder.Wait(); err != nil {
			errorsOut <- fmt.Errorf("media encoder process: %w", err)
		}
	}()
	go func() {
		errorsOut <- fmt.Errorf("encoded video output: %w", pump(videoOutput, video, 90000, 3000, &codecs.H264Payloader{}))
	}()
	go func() {
		errorsOut <- fmt.Errorf("encoded audio output: %w", pump(audioOutput, audio, 48000, 960, &codecs.OpusPayloader{}))
	}()
	startedAt := time.Now()
	go func() {
		poll := time.Second / 30
		if managed {
			// Check the newest native update twice per output slot. This
			// absorbs arrival jitter without sending more than 30 fps.
			poll /= 2
		}
		timer := time.NewTicker(poll)
		defer timer.Stop()
		var sent uint64
		var lastSent time.Time
		var lastIndex uint32
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-timer.C:
				index := uint32((now.Sub(startedAt) + time.Second/60) * 30 / time.Second)
				if !lastSent.IsZero() && int32(index-lastIndex) <= 0 {
					continue
				}
				// The native source only publishes on an update. Avoid sending
				// the same full-resolution pixels at 30 fps for every idle user.
				pixels, generation := buffer.pinFrameAfter(sent, !managed || now.Sub(lastSent) >= 500*time.Millisecond)
				if pixels == nil {
					continue
				}
				var header [4]byte
				binary.BigEndian.PutUint32(header[:], index)
				if _, err := vw.Write(header[:]); err != nil {
					buffer.unpinFrame()
					errorsOut <- fmt.Errorf("video input to encoder: %w", err)
					return
				}
				_, err := vw.Write(pixels)
				buffer.unpinFrame()
				sent, lastSent, lastIndex = generation, now, index
				if err != nil {
					errorsOut <- fmt.Errorf("video input to encoder: %w", err)
					return
				}
			}
		}
	}()
	go func() {
		timer := time.NewTicker(20 * time.Millisecond)
		defer timer.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case now := <-timer.C:
				var chunk [audioChunk]byte
				buffer.mu.Lock()
				buffer.audio.render(now, chunk[:])
				buffer.mu.Unlock()
				if _, err := aw.Write(chunk[:]); err != nil {
					errorsOut <- fmt.Errorf("audio input to encoder: %w", err)
					return
				}
			}
		}
	}()
	stats := time.NewTicker(time.Second)
	defer stats.Stop()
	last, at := buffer.updates.Load(), time.Now()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-errorsOut:
			return err
		case now := <-stats.C:
			total := buffer.updates.Load()
			report(fmt.Sprintf("source-fps:%.1f", float64(total-last)/now.Sub(at).Seconds()))
			last, at = total, now
		}
	}
}

func main() {
	program, err := os.Executable()
	if err != nil {
		log.Fatal(err)
	}
	sibling := func(name string) string { return filepath.Join(filepath.Dir(program), name) }
	bind := flag.String("listen", "127.0.0.1:6083", "browser listen IPv4 and port")
	target := flag.String("target", "", "private RDP address")
	port := flag.Uint("port", 3389, "RDP port")
	cert := flag.String("certificate", "", "pinned RDP PEM")
	lanCIDR := flag.String("lan-cidr", "", "allowed private IPv4 subnet; enables HTTPS LAN mode")
	authority := flag.String("authority", "", "HTTPS hostname with port, or IPv4 with port")
	tlsCert := flag.String("tls-cert", "", "HTTPS certificate with its intermediate chain")
	tlsKey := flag.String("tls-key", "", "HTTPS private key")
	udpPort := flag.Uint("media-port", 8443, "WebRTC UDP port on the same LAN address")
	maxSessions := flag.Int("max-sessions", 2, "simultaneous desktops on this Mac (1–64; size for the host)")
	statusSocket := flag.String("management-socket", "", "private local management socket")
	check := flag.Bool("check-config", false, "validate settings and TLS files without opening ports")
	source := flag.String("source", sibling("zmacrds-web-source"), "absolute path to the desktop adapter")
	encoder := flag.String("encoder", sibling("zmacrds-web-encode"), "absolute path to the ZMacRDS media encoder")
	flag.Parse()
	if *maxSessions < 1 || *maxSessions > 64 {
		log.Fatal("max-sessions must be between 1 and 64")
	}
	bindIP, bindPort, err := net.SplitHostPort(*bind)
	ip := net.ParseIP(*target)
	if err != nil || ip.To4() == nil || (!ip.IsPrivate() && !ip.IsLoopback()) ||
		*port < 1024 || *port > 65535 || !filepath.IsAbs(*cert) || !filepath.IsAbs(*source) || !filepath.IsAbs(*encoder) {
		log.Fatal("A private IPv4 target and absolute certificate, adapter and encoder paths are required")
	}
	if n, err := strconv.Atoi(bindPort); err != nil || n < 1024 || n > 65535 || strconv.Itoa(n) != bindPort || strings.TrimSpace(*target) != *target {
		log.Fatal("Invalid address or port")
	}
	address, err := netip.ParseAddr(bindIP)
	if err != nil {
		log.Fatal("Invalid listen address")
	}
	host, scheme := *bind, "http"
	var lan netip.Prefix
	var config *tls.Config
	if *lanCIDR != "" {
		if *target != "127.0.0.1" || *udpPort < 1024 || *udpPort > 65535 {
			log.Fatal("LAN mode requires the local RDP endpoint and a valid UDP media port")
		}
		lan, config, err = lanConfig(address, bindPort, *lanCIDR, *authority, *tlsCert, *tlsKey)
		if err != nil {
			log.Fatal(err)
		}
		host, scheme = *authority, "https"
	} else if bindIP != "127.0.0.1" || *authority != "" || *tlsCert != "" || *tlsKey != "" || *statusSocket != "" {
		log.Fatal("Use loopback for HTTP or configure LAN HTTPS explicitly")
	}
	if *check {
		return
	}
	var udp net.PacketConn
	if lan.IsValid() {
		packet, err := net.ListenPacket("udp4", net.JoinHostPort(bindIP, fmt.Sprint(*udpPort)))
		if err != nil {
			log.Fatal(err)
		}
		defer packet.Close()
		udp = subnetPacketConn{PacketConn: packet, lan: lan}
	}
	api, err := newAPI(address, udp)
	if err != nil {
		log.Fatal(err)
	}
	g := &gateway{host: host, origin: scheme + "://" + host, lan: lan,
		target: *target, port: fmt.Sprint(*port), cert: *cert, source: *source, encoder: *encoder, api: api, slots: make(chan struct{}, *maxSessions)}
	if *statusSocket != "" {
		listener, err := g.sessions.listen(*statusSocket)
		if err != nil {
			log.Fatal(err)
		}
		defer listener.Close()
	}
	server := &http.Server{Addr: *bind, Handler: http.HandlerFunc(g.serve), TLSConfig: config, ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout: 7 * time.Second, WriteTimeout: 22 * time.Second, IdleTimeout: 15 * time.Second, MaxHeaderBytes: 16 << 10}
	log.Printf("Browser desktop: %s", g.origin)
	if lan.IsValid() {
		g.attempts = &attemptLimiter{}
		g.localAttempts = localAttemptLimiter(*maxSessions)
		if name, _, _ := net.SplitHostPort(host); net.ParseIP(name) == nil {
			g.apple = &appleFlow{root: rootManaged}
		}
		log.Fatal(server.ListenAndServeTLS("", ""))
	}
	log.Fatal(server.ListenAndServe())
}
