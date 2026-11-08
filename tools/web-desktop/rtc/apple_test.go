package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func TestAppleLoginBindsCallbackAndMakesSingleUseGrant(t *testing.T) {
	const origin = "https://desktop.example.org:8443"
	var nonce string
	var exchanged int
	f := appleFlow{root: func(_ context.Context, req map[string]string) (managedReply, error) {
		if req["action"] == "managed-info" && req["origin"] == origin {
			return managedReply{OK: true, ServiceID: "test", RedirectURI: origin + "/apple/callback"}, nil
		}
		exchanged++
		if req["code"] != "fixture" || req["nonce"] != nonce || nonce == "" {
			t.Fatal("unexpected provider exchange")
		}
		return managedReply{OK: true, Username: "zr" + strings.Repeat("a", 24),
			Password: "A9-" + strings.Repeat("a", 43), ScreenKey: strings.Repeat("a", 64)}, nil
	}}
	start := httptest.NewRecorder()
	f.route(start, httptest.NewRequest("GET", origin+"/apple/start", nil), origin, func() bool { return true })
	location, err := url.Parse(start.Header().Get("Location"))
	if err != nil || start.Code != 303 || location.Host != "appleid.apple.com" {
		t.Fatal("expected redirect to Apple")
	}
	state := location.Query().Get("state")
	nonce = location.Query().Get("nonce")
	cookies := start.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != appleStateCookie+"-"+state || cookies[0].Value == state || !cookies[0].Secure ||
		!cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteNoneMode {
		t.Fatal("missing protected callback state")
	}
	callback := func(useCookie bool) *httptest.ResponseRecorder {
		body := url.Values{"state": {state}, "code": {"fixture"}}.Encode()
		req := httptest.NewRequest("POST", origin+"/apple/callback", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		if useCookie {
			req.AddCookie(cookies[0])
		}
		reply := httptest.NewRecorder()
		f.route(reply, req, origin, func() bool { return true })
		return reply
	}
	if callback(false).Code != 403 || exchanged != 0 {
		t.Fatal("callback without browser state reached provider exchange")
	}
	accepted := callback(true)
	next, err := url.Parse(accepted.Header().Get("Location"))
	if err != nil || next.Query().Get("apple") != "ready" || exchanged != 1 {
		t.Fatal("correct callback did not reach desktop")
	}
	selector := strings.TrimPrefix(next.Fragment, "login=")
	var grant *http.Cookie
	for _, cookie := range accepted.Result().Cookies() {
		if cookie.Name == appleGrantCookie+"-"+selector {
			grant = cookie
		}
	}
	if grant == nil || grant.Value == selector || !grant.Secure || !grant.HttpOnly || strings.Contains(accepted.Body.String(), "A9-") {
		t.Fatal("browser should get only a protected opaque ticket")
	}
	req := httptest.NewRequest("POST", origin+"/session", nil)
	req.AddCookie(grant)
	if _, ok := f.take(httptest.NewRecorder(), req, selector); !ok {
		t.Fatal("ticket should work once")
	}
	if _, ok := f.take(httptest.NewRecorder(), req, selector); ok {
		t.Fatal("ticket replay was accepted")
	}
	if callback(true).Header().Get("Location") != "/?apple=failed" || exchanged != 1 {
		t.Fatal("callback replay was accepted")
	}
}

func TestManagedGrantsCannotCrossBetweenTabs(t *testing.T) {
	const origin = "https://desktop.example.org:8443"
	f := &appleFlow{}
	issue := func(user string) (string, *http.Cookie) {
		w := httptest.NewRecorder()
		id, ok := f.issue(w, managedReply{Username: user})
		if !ok {
			t.Fatal("unable to issue login")
		}
		return id, w.Result().Cookies()[0]
	}
	a, cookieA := issue("test-a")
	b, cookieB := issue("test-b")
	r := httptest.NewRequest("POST", origin+"/session", nil)
	r.AddCookie(cookieB)
	if _, ok := f.take(httptest.NewRecorder(), r, a); ok {
		t.Fatal("tab A used B's browser proof")
	}
	wrong := httptest.NewRequest("POST", origin+"/session", nil)
	wrong.AddCookie(&http.Cookie{Name: cookieA.Name, Value: cookieB.Value})
	if _, ok := f.take(httptest.NewRecorder(), wrong, a); ok {
		t.Fatal("a renamed browser proof was accepted")
	}
	r.AddCookie(cookieA)
	for id, user := range map[string]string{a: "test-a", b: "test-b"} {
		if login, ok := f.take(httptest.NewRecorder(), r, id); !ok || login.Username != user {
			t.Fatalf("one browser's cookies lost or crossed the grants")
		}
	}
}
