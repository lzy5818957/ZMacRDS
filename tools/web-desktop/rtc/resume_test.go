package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestThirtyTwoLocalUsersReserveIndependentTransports(t *testing.T) {
	const capacity = 32
	g := &gateway{slots: make(chan struct{}, capacity)}
	type result struct {
		transport *desktopTransport
		login     managedReply
	}
	connections := make(chan result, capacity)
	for i := 0; i < capacity; i++ {
		go func(i int) {
			request := httptest.NewRequest("POST", "/session", nil)
			next, _, _, login, code := g.attach(httptest.NewRecorder(), request,
				offer{Username: "user" + strconv.Itoa(i), Password: "fixture"})
			if code != 0 {
				t.Errorf("user %d rejected with %d", i, code)
			}
			connections <- result{next, login}
		}(i)
	}
	rows := make(map[string]*desktopTransport, capacity)
	for i := 0; i < capacity; i++ {
		connection := <-connections
		if connection.transport != nil {
			rows[connection.login.Username] = connection.transport
		}
	}
	if len(rows) != capacity || g.reserve() {
		t.Fatal("32 distinct desktop transports did not fill the configured capacity")
	}
	for _, transport := range rows {
		transport.cancel()
		g.releaseTransport(nil, transport)
	}
	if len(g.slots) != 0 {
		t.Fatal("a disconnected transport kept its slot")
	}
}

func resumeFixture(t *testing.T, g *gateway, user string) (*desktopLease, *desktopTransport, *http.Cookie) {
	t.Helper()
	grantRecorder := httptest.NewRecorder()
	grant, ok := g.apple.issue(grantRecorder, managedReply{Username: user, Password: "test-server-only-password", ScreenKey: "test-pin"})
	if !ok {
		t.Fatal("grant unavailable")
	}
	req := httptest.NewRequest("POST", "/session", nil)
	req.AddCookie(grantRecorder.Result().Cookies()[0])
	w := httptest.NewRecorder()
	peer, lease, _, _, status := g.attach(w, req, offer{Managed: true, Grant: grant})
	if status != 0 {
		t.Fatal(status)
	}
	g.markDesktopReady(lease, peer)
	return lease, peer, w.Result().Cookies()[1]
}

func TestFullServerTransfersEachUsersOwnReservation(t *testing.T) {
	g := &gateway{apple: &appleFlow{}, slots: make(chan struct{}, 5)}
	type row struct {
		lease  *desktopLease
		peer   *desktopTransport
		cookie *http.Cookie
	}
	var users []row
	for i := 0; i < 5; i++ {
		l, p, c := resumeFixture(t, g, string(rune('A'+i)))
		users = append(users, row{l, p, c})
	}
	if g.reserve() {
		t.Fatal("sixth desktop unexpectedly admitted")
	}
	for i := 0; i < 2; i++ {
		wrong := httptest.NewRequest("POST", "/session", nil)
		wrong.AddCookie(users[1-i].cookie)
		if _, _, _, _, code := g.attach(httptest.NewRecorder(), wrong, offer{Managed: true, Resume: users[i].lease.id}); code != 401 {
			t.Fatal("different user's proof accepted", code)
		}
	}
	results := make(chan error, 2)
	for i := 0; i < 2; i++ {
		u := users[i]
		go func() {
			r := httptest.NewRequest("POST", "/session", nil)
			r.AddCookie(u.cookie)
			next, lease, old, login, code := g.attach(httptest.NewRecorder(), r, offer{Managed: true, Resume: u.lease.id})
			if code != 0 || old != u.peer || login.Username != u.lease.login.Username {
				results <- context.Canceled
				return
			}
			go func() { <-old.ctx.Done(); g.releaseTransport(lease, old) }()
			if !g.handoff(lease, next, old, r.Context()) {
				results <- context.Canceled
				return
			}
			results <- nil
		}()
	}
	for i := 0; i < 2; i++ {
		select {
		case err := <-results:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(time.Second):
			t.Fatal("handoff hung")
		}
	}
	if len(g.slots) != 5 {
		t.Fatal("capacity changed", len(g.slots))
	}
	for _, u := range users[2:] {
		if u.peer.ctx.Err() != nil || u.lease.current != u.peer {
			t.Fatal("other user's desktop changed")
		}
	}
	for _, u := range users {
		g.releaseTransport(u.lease, u.lease.current)
	}
	if len(g.slots) != 0 {
		t.Fatal("slot leaked")
	}
}

func TestNewLoginReplacesOnlySameUsersReservationAtCapacity(t *testing.T) {
	g := &gateway{apple: &appleFlow{}, slots: make(chan struct{}, 2)}
	previous, previousPeer, previousCookie := resumeFixture(t, g, "A")
	other, otherPeer, _ := resumeFixture(t, g, "B")

	w := httptest.NewRecorder()
	grant, ok := g.apple.issue(w, previous.login)
	if !ok {
		t.Fatal("grant unavailable")
	}
	req := httptest.NewRequest("POST", "/session", nil)
	req.AddCookie(w.Result().Cookies()[0])
	next, lease, old, login, status := g.attach(httptest.NewRecorder(), req, offer{Managed: true, Grant: grant})
	if status != 0 || old != previousPeer || lease.id == previous.id || login.Username != "A" {
		t.Fatalf("same account did not reclaim its reservation: status %d", status)
	}
	resume := httptest.NewRequest("POST", "/session", nil)
	resume.AddCookie(previousCookie)
	if _, _, _, _, code := g.attach(httptest.NewRecorder(), resume, offer{Managed: true, Resume: previous.id}); code != 401 {
		t.Fatal("previous browser could still resume", code)
	}

	go func() { <-old.ctx.Done(); g.releaseTransport(previous, old) }()
	if !g.handoff(lease, next, old, req.Context()) || otherPeer.ctx.Err() != nil || len(g.slots) != 2 {
		t.Fatal("same-user replacement changed the other desktop or the capacity")
	}
	next.cancel()
	g.releaseTransport(lease, next)
	otherPeer.cancel()
	g.releaseTransport(other, otherPeer)
}

func TestResumeAfterLossAndExplicitRevocation(t *testing.T) {
	g := &gateway{origin: "https://desktop.test", apple: &appleFlow{}, slots: make(chan struct{}, 1)}
	l, p, c := resumeFixture(t, g, "A")
	p.cancel()
	g.releaseTransport(l, p)
	r := httptest.NewRequest("POST", "/session", nil)
	r.AddCookie(c)
	next, lease, old, login, code := g.attach(httptest.NewRecorder(), r, offer{Managed: true, Resume: l.id})
	if code != 0 || old != nil || login.Username != "A" {
		t.Fatal(code)
	}
	end := func(origin string) int {
		req := httptest.NewRequest("POST", "/session/end", strings.NewReader(`{"resume":"`+l.id+`"}`))
		req.Header.Set("Origin", origin)
		req.Header.Set("Content-Type", "application/json")
		req.AddCookie(c)
		w := httptest.NewRecorder()
		g.endDesktop(w, req)
		return w.Code
	}
	if end("https://different.test") != 403 || next.ctx.Err() != nil {
		t.Fatal("foreign origin ended session")
	}
	if end(g.origin) != 204 || next.ctx.Err() == nil {
		t.Fatal("disconnect did not revoke")
	}
	g.releaseTransport(lease, next)
	if _, _, _, _, code = g.attach(httptest.NewRecorder(), r, offer{Managed: true, Resume: l.id}); code != 401 {
		t.Fatal("revoked lease reused", code)
	}
}
