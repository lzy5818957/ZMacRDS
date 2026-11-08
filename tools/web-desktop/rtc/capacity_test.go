package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strconv"
	"testing"
	"time"

	"github.com/pion/webrtc/v4"
)

// Use the real HTTP negotiation and ICE/DTLS/SCTP transports, but hold the
// startup queue so the test needs no macOS accounts or capture processes.
func TestThirtyTwoSimultaneousWebRTCConnections(t *testing.T) {
	const capacity = 32
	api, err := newAPI(netip.MustParseAddr("127.0.0.1"), nil)
	if err != nil {
		t.Fatal(err)
	}
	// A browser pool sharing a single client IP also exercises the production
	// LAN rate limiter. The 33rd request should hit capacity, not a rate limit.
	g := &gateway{api: api, slots: make(chan struct{}, capacity), localAttempts: localAttemptLimiter(capacity)}
	release, err := g.startup.acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	server := httptest.NewUnstartedServer(http.HandlerFunc(g.serve))
	g.host = server.Listener.Addr().String()
	g.origin = "http://" + g.host
	server.Start()
	defer server.Close()

	var settings webrtc.SettingEngine
	settings.SetIncludeLoopbackCandidate(true)
	settings.SetNetworkTypes([]webrtc.NetworkType{webrtc.NetworkTypeUDP4})
	clientAPI := webrtc.NewAPI(webrtc.WithSettingEngine(settings))
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	type result struct {
		peer *webrtc.PeerConnection
		err  error
	}
	results := make(chan result, capacity)
	connect := func(i int) (*webrtc.PeerConnection, error) {
		peer, err := clientAPI.NewPeerConnection(webrtc.Configuration{})
		if err != nil {
			return nil, err
		}
		done := make(chan struct{})
		channel, err := peer.CreateDataChannel("desktop-input", nil)
		if err != nil {
			return peer, err
		}
		channel.OnOpen(func() { close(done) })
		description, err := peer.CreateOffer(nil)
		if err != nil {
			return peer, err
		}
		ready := webrtc.GatheringCompletePromise(peer)
		if err = peer.SetLocalDescription(description); err != nil {
			return peer, err
		}
		select {
		case <-ready:
		case <-ctx.Done():
			return peer, ctx.Err()
		}
		payload, err := json.Marshal(map[string]string{
			"username": "capacity" + strconv.Itoa(i), "password": "fixture", "sdp": peer.LocalDescription().SDP,
		})
		if err != nil {
			return peer, err
		}
		request, err := http.NewRequestWithContext(ctx, http.MethodPost, server.URL+"/session", bytes.NewReader(payload))
		if err != nil {
			return peer, err
		}
		request.Header.Set("Origin", g.origin)
		request.Header.Set("Content-Type", "application/json")
		reply, err := server.Client().Do(request)
		if err != nil {
			return peer, err
		}
		defer reply.Body.Close()
		if reply.StatusCode != http.StatusOK {
			return peer, fmt.Errorf("signaling returned HTTP %d", reply.StatusCode)
		}
		var answer webrtc.SessionDescription
		if err = json.NewDecoder(reply.Body).Decode(&answer); err != nil {
			return peer, err
		}
		if err = peer.SetRemoteDescription(answer); err != nil {
			return peer, err
		}
		select {
		case <-done:
			return peer, nil
		case <-ctx.Done():
			return peer, ctx.Err()
		}
	}
	for i := range capacity {
		go func() {
			peer, err := connect(i)
			results <- result{peer, err}
		}()
	}
	for range capacity {
		got := <-results
		if got.peer != nil {
			t.Cleanup(func() { _ = got.peer.Close() })
		}
		if got.err != nil {
			t.Errorf("client could not establish its data channel: %v", got.err)
		}
	}
	if len(g.slots) != capacity {
		t.Fatalf("got %d live reservations, want %d", len(g.slots), capacity)
	}
	extra, err := connect(capacity)
	if extra != nil {
		_ = extra.Close()
	}
	if err == nil || err.Error() != "signaling returned HTTP 503" {
		t.Fatalf("33rd connection did not receive the capacity response: %v", err)
	}
}
