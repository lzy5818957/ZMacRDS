package main

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestStartupGateBoundsTheBurstAndDropsCancelledWaiters(t *testing.T) {
	var gate startupGate
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	release, err := gate.acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	short, abort := context.WithCancel(ctx)
	result := make(chan error, 1)
	go func() {
		release, err := gate.acquire(short)
		if release != nil {
			release()
		}
		result <- err
	}()
	abort()
	if err := <-result; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	opened := make(chan struct{})
	go func() {
		if next, err := gate.acquire(ctx); err == nil {
			close(opened)
			next()
		}
	}()
	select {
	case <-opened:
		t.Fatal("second startup ran while the first held the gate")
	case <-time.After(10 * time.Millisecond):
	}
	release()
	release()
	select {
	case <-opened:
	case <-ctx.Done():
		t.Fatal("the cancelled waiter blocked the queue")
	}
}
