package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
	"sync"
	"time"
)

const desktopLifetime = 8 * time.Hour
const resumeIdle = 5 * time.Minute
const resumeCookie = "__Host-zmacrds-resume-"

type desktopTransport struct {
	ctx         context.Context
	cancel      context.CancelFunc
	done        chan struct{}
	transferred bool
}

func newDesktopTransport() *desktopTransport {
	ctx, cancel := context.WithCancel(context.Background())
	return &desktopTransport{ctx: ctx, cancel: cancel, done: make(chan struct{})}
}

type desktopLease struct {
	id, proof        string
	login            managedReply
	until, idleUntil time.Time
	current          *desktopTransport
	ready, replacing bool
}

type desktopLeases struct {
	mu   sync.Mutex
	rows map[string]*desktopLease
}

func (l *desktopLease) expired(now time.Time) bool {
	return !now.Before(l.until) || (l.current == nil && !now.Before(l.idleUntil))
}

// The channel continues to bound media transports. An authenticated reload
// transfers its old reservation under the same lock that releases it.
func (g *gateway) attach(w http.ResponseWriter, r *http.Request, req offer) (*desktopTransport, *desktopLease, *desktopTransport, managedReply, int) {
	d := &g.leases
	d.mu.Lock()
	defer d.mu.Unlock()
	now := time.Now()
	for id, lease := range d.rows {
		if lease.expired(now) {
			delete(d.rows, id)
			if lease.current != nil {
				lease.current.cancel()
			}
		}
	}
	if req.Resume != "" {
		lease := d.rows[req.Resume]
		if lease == nil || !lease.ready || !lease.matches(r) {
			return nil, nil, nil, managedReply{}, http.StatusUnauthorized
		}
		if lease.replacing {
			return nil, nil, nil, managedReply{}, http.StatusConflict
		}
		old := lease.current
		if old != nil {
			old.transferred = true
			lease.replacing = true
		} else if !g.reserve() {
			return nil, nil, nil, managedReply{}, http.StatusServiceUnavailable
		}
		next := newDesktopTransport()
		lease.current = next
		return next, lease, old, lease.login, 0
	}
	if !req.Managed {
		if !g.reserve() {
			return nil, nil, nil, managedReply{}, http.StatusServiceUnavailable
		}
		return newDesktopTransport(), nil, nil, managedReply{Username: req.Username, Password: req.Password}, 0
	}
	login, ok := g.apple.take(w, r, req.Grant)
	if !ok {
		return nil, nil, nil, managedReply{}, http.StatusUnauthorized
	}
	// A newly authenticated login to the same account may take over its
	// reservation, even on a full Mac. Revoke the old browser's resume proof.
	var old *desktopTransport
	var previous *desktopLease
	for _, entry := range d.rows {
		if entry.login.Username == login.Username {
			previous = entry
			break
		}
	}
	if previous != nil && previous.replacing {
		return nil, nil, nil, managedReply{}, http.StatusConflict
	}
	// Bound retained credentials even if many different accounts visit.
	if previous == nil && len(d.rows) >= 128 {
		return nil, nil, nil, managedReply{}, http.StatusServiceUnavailable
	}
	if previous != nil {
		old = previous.current
	}
	if old != nil {
		old.transferred = true
	} else if !g.reserve() {
		return nil, nil, nil, managedReply{}, http.StatusServiceUnavailable
	}
	if previous != nil {
		delete(d.rows, previous.id)
	}
	next := newDesktopTransport()
	lease := &desktopLease{id: randomAppleKey(), proof: randomAppleKey(), login: login,
		until: now.Add(desktopLifetime), current: next, replacing: old != nil}
	if d.rows == nil {
		d.rows = make(map[string]*desktopLease)
	}
	d.rows[lease.id] = lease
	// An inactive desktop can be resumed for a short interval after losing
	// transport. Expiry is fixed from sign-in, never extended by reload.
	appleCookie(w, resumeCookie+lease.id, lease.proof, int(desktopLifetime/time.Second), http.SameSiteStrictMode)
	return next, lease, old, login, 0
}

func (g *gateway) reserve() bool {
	select {
	case g.slots <- struct{}{}:
		return true
	default:
		return false
	}
}

func (g *gateway) handoff(lease *desktopLease, next, old *desktopTransport, request context.Context) bool {
	if old == nil {
		return true
	}
	old.cancel()
	// Do not start another encoder or native desktop source until the old
	// processes have exited. Keep the reservation even if the request ends.
	<-old.done
	g.leases.mu.Lock()
	defer g.leases.mu.Unlock()
	lease.replacing = false
	return request.Err() == nil && next.ctx.Err() == nil
}

func (g *gateway) releaseTransport(lease *desktopLease, transport *desktopTransport) {
	g.leases.mu.Lock()
	defer g.leases.mu.Unlock()
	if lease != nil && lease.current == transport {
		lease.current = nil
		lease.idleUntil = time.Now().Add(resumeIdle)
		if !lease.ready || lease.expired(time.Now()) {
			delete(g.leases.rows, lease.id)
		} else {
			delay := min(time.Until(lease.until), resumeIdle)
			time.AfterFunc(delay, func() {
				g.leases.mu.Lock()
				defer g.leases.mu.Unlock()
				if g.leases.rows[lease.id] == lease && lease.current == nil && lease.expired(time.Now()) {
					delete(g.leases.rows, lease.id)
				}
			})
		}
	}
	if !transport.transferred {
		<-g.slots
	}
	close(transport.done)
}

func (g *gateway) markDesktopReady(lease *desktopLease, transport *desktopTransport) {
	if lease == nil {
		return
	}
	g.leases.mu.Lock()
	defer g.leases.mu.Unlock()
	if lease.current == transport && transport.ctx.Err() == nil {
		lease.ready = true
	}
}

func (l *desktopLease) matches(r *http.Request) bool {
	cookie, err := r.Cookie(resumeCookie + l.id)
	return err == nil && subtle.ConstantTimeCompare([]byte(cookie.Value), []byte(l.proof)) == 1
}

func (g *gateway) endDesktop(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Origin") != g.origin || r.Header.Get("Content-Type") != "application/json" {
		http.Error(w, "Forbidden", http.StatusForbidden)
		return
	}
	var req struct {
		Resume string `json:"resume"`
	}
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 256))
	dec.DisallowUnknownFields()
	if dec.Decode(&req) != nil || dec.Decode(new(any)) != io.EOF || !appleSelector.MatchString(req.Resume) {
		http.Error(w, "Invalid request", http.StatusBadRequest)
		return
	}
	g.leases.mu.Lock()
	defer g.leases.mu.Unlock()
	if lease := g.leases.rows[req.Resume]; lease != nil && lease.matches(r) {
		delete(g.leases.rows, lease.id)
		if lease.current != nil {
			lease.current.cancel()
		}
		appleCookie(w, resumeCookie+lease.id, "", -1, http.SameSiteStrictMode)
	}
	w.WriteHeader(http.StatusNoContent)
}
