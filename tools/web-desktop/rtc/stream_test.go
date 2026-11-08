package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/rtp/codecs"
)

func TestSlowEncoderKeepsItsFrameWhileNewFramesArrive(t *testing.T) {
	var media mediaBuffer
	frame := make([]byte, 5+frameBytes)
	frame[0] = 1
	binary.BigEndian.PutUint32(frame[1:5], frameBytes)
	publish := func(value byte) {
		frame[5], frame[len(frame)-1] = value, value
		if err := media.read(bytes.NewReader(frame)); !errors.Is(err, io.EOF) {
			t.Fatal(err)
		}
	}
	publish(1)
	held := media.pinFrame()
	for value := byte(2); value <= 5; value++ {
		publish(value)
	}
	if held[0] != 1 || held[len(held)-1] != 1 {
		t.Fatal("the writer's frame was overwritten while pinned")
	}
	media.unpinFrame()
	last := media.pinFrame()
	defer media.unpinFrame()
	if last[0] != 5 || last[len(last)-1] != 5 || media.updates.Load() != 5 {
		t.Fatal("the encoder did not get the newest complete frame")
	}
}

func TestDesktopSourceExitBeforeFirstFrame(t *testing.T) {
	source := filepath.Join(t.TempDir(), "source")
	if err := os.WriteFile(source, []byte("#!/bin/sh\nIFS= read -r login\n"), 0700); err != nil {
		t.Fatal(err)
	}
	g := gateway{source: source}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	err := g.stream(ctx, nil, nil, []byte("fixture"), nil, func(string) {})
	if !errors.Is(err, io.EOF) || !strings.HasPrefix(err.Error(), "desktop source output:") {
		t.Fatalf("expected the desktop source to identify its early exit, got %v", err)
	}
}

func TestMediaSurvivesEncoderDelayAndDuplicate(t *testing.T) {
	var input bytes.Buffer
	for _, index := range []uint32{1, 1, 200, 202} {
		var header [8]byte
		binary.BigEndian.PutUint32(header[:4], 1)
		binary.BigEndian.PutUint32(header[4:], index)
		input.Write(header[:])
		input.WriteByte(byte(index))
	}
	var stamps []uint32
	err := pumpPackets(&input, func(packet *rtp.Packet) error {
		stamps = append(stamps, packet.Timestamp)
		return nil
	}, 90000, 3000, &codecs.OpusPayloader{})
	if !errors.Is(err, io.EOF) || len(stamps) != 3 || stamps[1]-stamps[0] != 199*3000 || stamps[2]-stamps[1] != 2*3000 {
		t.Fatalf("unexpected timestamps after a stall and a duplicate: %v, %v", stamps, err)
	}
}
