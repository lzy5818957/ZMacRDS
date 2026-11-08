package main

import (
	"crypto/tls"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"testing"
	"time"
)

func TestLANHTTPRequiresTLSAuthoritySubnetAndOrigin(t *testing.T) {
	g := gateway{host: "desktop.example.test:8443", origin: "https://desktop.example.test:8443",
		lan: netip.MustParsePrefix("192.168.42.0/24")}
	for _, tc := range []struct {
		name, host, peer, origin string
		secure                   bool
		code                     int
	}{
		{"right page", g.host, "192.168.42.55:49152", "", true, 200},
		{"plain HTTP", g.host, "192.168.42.55:49152", "", false, 403},
		{"foreign subnet", g.host, "192.168.43.55:49152", "", true, 403},
		{"wrong host", "attacker.test:8443", "192.168.42.55:49152", "", true, 403},
		{"clear origin", g.host, "192.168.42.55:49152", "http://" + g.host, true, 403},
		{"foreign origin", g.host, "192.168.42.55:49152", "https://attacker.test:8443", true, 403},
		{"correct origin reaches JSON validation", g.host, "192.168.42.55:49152", g.origin, true, 400},
	} {
		t.Run(tc.name, func(t *testing.T) {
			method, path := http.MethodGet, "/"
			if tc.origin != "" {
				method, path = http.MethodPost, "/session"
			}
			r := httptest.NewRequest(method, path, nil)
			r.Host, r.RemoteAddr = tc.host, tc.peer
			if tc.secure {
				r.TLS = &tls.ConnectionState{}
			}
			r.Header.Set("Origin", tc.origin)
			r.Header.Set("Content-Type", "application/json")
			r.Header.Set("X-Forwarded-For", "192.168.42.55")
			w := httptest.NewRecorder()
			g.serve(w, r)
			if w.Code != tc.code {
				t.Fatalf("status = %d; expected %d", w.Code, tc.code)
			}
		})
	}
}

func TestLANAttemptsLimitedIndependently(t *testing.T) {
	l := &attemptLimiter{}
	a, b := netip.MustParseAddr("192.168.42.2"), netip.MustParseAddr("192.168.42.3")
	now := time.Now()
	for i := 0; i < 6; i++ {
		if !l.allow(a, now) {
			t.Fatal("blocked a legitimate attempt")
		}
	}
	if l.allow(a, now) || !l.allow(b, now) || !l.allow(a, now.Add(time.Minute)) {
		t.Fatal("retry window is incorrect or one client limits another")
	}
}
