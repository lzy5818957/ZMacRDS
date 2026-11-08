package main

import (
	"bytes"
	"testing"
	"time"
)

func TestPCMUsesCaptureOrderAcrossTimestampWrapAndArrivalJitter(t *testing.T) {
	var queue pcmTimeline
	now := time.Unix(1, 0)
	stamp := uint32(0xfffffff4)
	first := bytes.Repeat([]byte{1, 2, 3, 4}, 960)
	second := bytes.Repeat([]byte{5, 6, 7, 8}, 960)
	queue.insert(now, stamp, first)
	silence := make([]byte, audioChunk)
	out := make([]byte, audioChunk)
	queue.render(now.Add(pcmDelay-time.Millisecond), out)
	if !bytes.Equal(out, silence) {
		t.Fatal("played before the fixed presentation time")
	}
	queue.render(now.Add(pcmDelay), out)
	if !bytes.Equal(out, first) {
		t.Fatal("first packet shifted")
	}
	queue.insert(now.Add(49*time.Millisecond), stamp+20, second)
	clear(out)
	queue.render(now.Add(pcmDelay+20*time.Millisecond), out)
	if !bytes.Equal(out, second) {
		t.Fatal("network jitter shifted the second packet")
	}
	clear(out)
	queue.render(now.Add(pcmDelay+40*time.Millisecond), out)
	if !bytes.Equal(out, silence) {
		t.Fatal("an underrun must be silent")
	}
}

func TestPCMDoesNotReplayLateSamplesAfterStall(t *testing.T) {
	var queue pcmTimeline
	now := time.Unix(1, 0)
	queue.insert(now, 1000, bytes.Repeat([]byte{1, 2, 3, 4}, 960))
	out := make([]byte, audioChunk)
	queue.render(now.Add(pcmDelay+200*time.Millisecond), out)
	if !bytes.Equal(out, make([]byte, len(out))) {
		t.Fatal("replayed stale sound after a stalled encoder")
	}
}
