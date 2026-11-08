package main

import (
	"context"
	"sync"
	"time"
)

const startupQueueLimit = 6 * time.Minute
const desktopWarmup = 2 * time.Second

// Screen Sharing creates Aqua sessions, decoders and hardware encoders on
// the same Mac. Bound that initial burst separately from running desktops.
type startupGate struct {
	init  sync.Once
	token chan struct{}
}

func (g *startupGate) acquire(ctx context.Context) (func(), error) {
	g.init.Do(func() { g.token = make(chan struct{}, 1) })
	select {
	case g.token <- struct{}{}:
		// A cancelled waiter can win the select at the same time as a slot
		// opens. Don't start any work for a dead browser.
		if err := ctx.Err(); err != nil {
			<-g.token
			return nil, err
		}
		var once sync.Once
		return func() { once.Do(func() { <-g.token }) }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}
