package main

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

const appleStateCookie = "__Host-zmacrds-apple-state"
const appleGrantCookie = "__Host-zmacrds-apple-grant"
const appleAuthorize = "https://appleid.apple.com/auth/authorize"

var managedName = regexp.MustCompile("^zr[a-f0-9]{24}$")
var managedPassword = regexp.MustCompile("^A9-[A-Za-z0-9_-]{43}$")
var nativeKeyPin = regexp.MustCompile("^[a-f0-9]{64}$")
var appleSelector = regexp.MustCompile("^[A-Za-z0-9_-]{43}$")

type managedReply struct {
	OK          bool   `json:"ok"`
	ServiceID   string `json:"service_id"`
	RedirectURI string `json:"redirect_uri"`
	Username    string `json:"username"`
	Password    string `json:"password"`
	ScreenKey   string `json:"screen_key_sha256"`
}

func validManagedLogin(login managedReply) bool {
	return login.OK && managedName.MatchString(login.Username) &&
		managedPassword.MatchString(login.Password) && nativeKeyPin.MatchString(login.ScreenKey)
}

func rootManaged(ctx context.Context, req map[string]string) (managedReply, error) {
	var reply managedReply
	dialer := net.Dialer{Timeout: time.Second}
	conn, err := dialer.DialContext(ctx, "unix", "/var/run/org.zmacrds.control.sock")
	if err != nil {
		return reply, err
	}
	defer conn.Close()
	f, err := conn.(*net.UnixConn).SyscallConn()
	if err != nil {
		return reply, err
	}
	var peer *unix.Xucred
	var peerErr error
	if err = f.Control(func(fd uintptr) { peer, peerErr = unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED) }); err != nil ||
		peerErr != nil || peer == nil || peer.Uid != 0 {
		return reply, errors.New("untrusted local controller")
	}
	_ = conn.SetDeadline(time.Now().Add(65 * time.Second))
	if err = json.NewEncoder(conn).Encode(req); err != nil {
		return reply, err
	}
	line, err := bufio.NewReader(io.LimitReader(conn, 4096)).ReadBytes('\n')
	if err != nil {
		return reply, err
	}
	return reply, json.Unmarshal(line, &reply)
}

type transient[T any] struct {
	value T
	until time.Time
}

type temporary[T any] struct {
	mu    sync.Mutex
	items map[string]transient[T]
}

func randomAppleKey() string {
	data := make([]byte, 32)
	if _, err := rand.Read(data); err != nil {
		panic("OS random source failed")
	}
	return base64.RawURLEncoding.EncodeToString(data)
}

func (t *temporary[T]) put(key string, value T, duration time.Duration) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	now := time.Now()
	if t.items == nil {
		t.items = map[string]transient[T]{}
	}
	for k, v := range t.items {
		if !now.Before(v.until) {
			delete(t.items, k)
		}
	}
	if len(t.items) >= 128 {
		return false
	}
	t.items[key] = transient[T]{value, now.Add(duration)}
	return true
}

func (t *temporary[T]) take(key string, match func(T) bool) (T, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	row, ok := t.items[key]
	if !ok || time.Now().After(row.until) || !match(row.value) {
		var empty T
		return empty, false
	}
	delete(t.items, key)
	return row.value, true
}

type appleState struct{ nonce, proof string }
type appleGrant struct {
	login managedReply
	proof string
}

type appleFlow struct {
	root     func(context.Context, map[string]string) (managedReply, error)
	states   temporary[appleState]
	grants   temporary[appleGrant]
	infoMu   sync.Mutex
	last     transient[managedReply]
	activeMu sync.Mutex
	active   map[string]*context.CancelFunc
}

func appleCookie(w http.ResponseWriter, name, value string, age int, mode http.SameSite) {
	http.SetCookie(w, &http.Cookie{Name: name, Value: value, Path: "/", HttpOnly: true, Secure: true, MaxAge: age, SameSite: mode})
}

func (a *appleFlow) issue(w http.ResponseWriter, login managedReply) (string, bool) {
	selector, proof := randomAppleKey(), randomAppleKey()
	if !a.grants.put(selector, appleGrant{login, proof}, 90*time.Second) {
		return "", false
	}
	appleCookie(w, appleGrantCookie+"-"+selector, proof, 90, http.SameSiteStrictMode)
	return selector, true
}

func (a *appleFlow) info(ctx context.Context, origin string) (managedReply, bool) {
	a.infoMu.Lock()
	defer a.infoMu.Unlock()
	if time.Now().After(a.last.until) {
		info, err := a.root(ctx, map[string]string{"action": "managed-info", "origin": origin})
		if err != nil || !info.OK || info.ServiceID == "" || info.RedirectURI != origin+"/apple/callback" {
			info = managedReply{}
		}
		a.last = transient[managedReply]{info, time.Now().Add(20 * time.Second)}
	}
	return a.last.value, a.last.value.OK
}

// One keyboard owner per immutable company account. The old peer is released
// only when the new peer has authenticated and connected.
func (a *appleFlow) takeControl(username string, cancel context.CancelFunc) func() {
	a.activeMu.Lock()
	if a.active == nil {
		a.active = make(map[string]*context.CancelFunc)
	}
	previous := a.active[username]
	a.active[username] = &cancel
	a.activeMu.Unlock()
	if previous != nil {
		(*previous)()
	}
	return func() {
		a.activeMu.Lock()
		if a.active[username] == &cancel {
			delete(a.active, username)
		}
		a.activeMu.Unlock()
	}
}

func (a *appleFlow) route(w http.ResponseWriter, r *http.Request, origin string, allow func() bool) {
	switch {
	case r.URL.Path == "/apple/config" && r.Method == http.MethodGet:
		_, ok := a.info(r.Context(), origin)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]bool{"enabled": ok})
	case r.URL.Path == "/apple/start" && r.Method == http.MethodGet:
		if !allow() {
			http.Error(w, "尝试次数过多，请稍后再试。", 429)
			return
		}
		info, ok := a.info(r.Context(), origin)
		if !ok {
			http.Error(w, "受管理 Apple 登录尚未配置。", 503)
			return
		}
		state, nonce, proof := randomAppleKey(), randomAppleKey(), randomAppleKey()
		if !a.states.put(state, appleState{nonce, proof}, 5*time.Minute) {
			http.Error(w, "登录忙，请稍后重试。", 503)
			return
		}
		appleCookie(w, appleStateCookie+"-"+state, proof, 300, http.SameSiteNoneMode)
		values := url.Values{"client_id": {info.ServiceID}, "redirect_uri": {info.RedirectURI},
			"response_type": {"code"}, "response_mode": {"form_post"}, "scope": {"email"}, "state": {state}, "nonce": {nonce}}
		http.Redirect(w, r, appleAuthorize+"?"+values.Encode(), http.StatusSeeOther)
	case r.URL.Path == "/apple/callback" && r.Method == http.MethodPost:
		r.Body = http.MaxBytesReader(w, r.Body, 16*1024)
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "application/x-www-form-urlencoded") || r.ParseForm() != nil {
			http.Error(w, "无效的 Apple 回调。", 400)
			return
		}
		state := r.PostForm.Get("state")
		if !appleSelector.MatchString(state) || len(r.PostForm["state"]) != 1 {
			http.Error(w, "登录已过期或无法确认。请回到首页重新发起。", 403)
			return
		}
		name := appleStateCookie + "-" + state
		cookie, err := r.Cookie(name)
		if err != nil || !appleSelector.MatchString(cookie.Value) {
			http.Error(w, "登录已过期或无法确认。请回到首页重新发起。", 403)
			return
		}
		appleCookie(w, name, "", -1, http.SameSiteNoneMode)
		saved, valid := a.states.take(state, func(s appleState) bool {
			return subtle.ConstantTimeCompare([]byte(s.proof), []byte(cookie.Value)) == 1
		})
		code := r.PostForm.Get("code")
		if !valid || code == "" || len(code) > 2048 || len(r.PostForm["code"]) != 1 || r.PostForm.Has("error") {
			http.Redirect(w, r, "/?apple=failed", http.StatusSeeOther)
			return
		}
		// The callback may create a local account. Bound the response even
		// though the ordinary media offer has a shorter server write timeout.
		_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(70 * time.Second))
		login, err := a.root(r.Context(), map[string]string{"action": "managed-exchange", "code": code, "nonce": saved.nonce})
		if err != nil || !validManagedLogin(login) {
			http.Redirect(w, r, "/?apple=failed", http.StatusSeeOther)
			return
		}
		selector, ok := a.issue(w, login)
		if !ok {
			http.Error(w, "桌面繁忙，请稍后重试。", 503)
			return
		}
		http.Redirect(w, r, "/?apple=ready#login="+selector, http.StatusSeeOther)
	default:
		http.NotFound(w, r)
	}
}

func (a *appleFlow) take(w http.ResponseWriter, r *http.Request, selector string) (managedReply, bool) {
	if !appleSelector.MatchString(selector) {
		return managedReply{}, false
	}
	name := appleGrantCookie + "-" + selector
	cookie, err := r.Cookie(name)
	if err != nil || !appleSelector.MatchString(cookie.Value) {
		return managedReply{}, false
	}
	appleCookie(w, name, "", -1, http.SameSiteStrictMode)
	grant, ok := a.grants.take(selector, func(g appleGrant) bool {
		return subtle.ConstantTimeCompare([]byte(g.proof), []byte(cookie.Value)) == 1
	})
	return grant.login, ok
}
