package main

import "time"

const (
	samplesPerMS = 48
	pcmWindow    = samplesPerMS * 500
	// The loopback pilot needs room for short PCM bursts without delaying
	// playback noticeably relative to the native video encoder.
	pcmDelay = 40 * time.Millisecond
)

// RDP Wave2 carries the guest's capture clock in milliseconds. A fixed-size
// timeline retains each packet's position even if its arrival is bursty.
type pcmTimeline struct {
	data        [pcmWindow * 4]byte
	guestStart  uint32
	hostStart   time.Time
	lastArrival time.Time
	read, end   int64
}

func (p *pcmTimeline) insert(now time.Time, stamp uint32, pcm []byte) {
	if p.hostStart.IsZero() || now.Sub(p.lastArrival) > 450*time.Millisecond {
		*p = pcmTimeline{guestStart: stamp, hostStart: now.Add(pcmDelay)}
	}
	p.lastArrival = now
	start := int64(int32(stamp-p.guestStart)) * samplesPerMS
	if delta := start - p.end; delta >= -96 && delta <= 96 {
		// The millisecond timestamp is rounded; short jitter in the
		// timestamps should not create gaps between adjacent PCM samples.
		start = p.end
	}
	if start < p.read || start+int64(len(pcm)/4) > p.read+pcmWindow {
		return
	}
	for i := start; i > p.end; {
		i--
		at := i % pcmWindow * 4
		clear(p.data[at : at+4])
	}
	for offset := 0; offset < len(pcm); offset += 4 {
		at := (start + int64(offset/4)) % pcmWindow * 4
		copy(p.data[at:at+4], pcm[offset:offset+4])
	}
	if end := start + int64(len(pcm)/4); end > p.end {
		p.end = end
	}
}

func (p *pcmTimeline) render(now time.Time, output []byte) {
	if p.hostStart.IsZero() || now.Before(p.hostStart) {
		return
	}
	shouldRead := now.Sub(p.hostStart).Milliseconds() * samplesPerMS
	if shouldRead > p.read+int64(len(output)/4) {
		// Skip late samples after a blocked encoder instead of replaying
		// an older audio queue out of sync with the newest frame.
		p.read = shouldRead
	}
	for offset := 0; offset < len(output); offset += 4 {
		if at := p.read % pcmWindow * 4; p.read < p.end {
			copy(output[offset:offset+4], p.data[at:at+4])
			clear(p.data[at : at+4])
		}
		p.read++
	}
	if p.read > p.end {
		p.end = p.read
	}
}
