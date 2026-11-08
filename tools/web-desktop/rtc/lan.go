package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"net"
	"net/netip"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

var dnsLabel = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`)

func lanConfig(address netip.Addr, port, subnet, authority, cert, key string) (netip.Prefix, *tls.Config, error) {
	prefix, err := netip.ParsePrefix(subnet)
	if err != nil || !address.Is4() || !address.IsPrivate() || !prefix.Addr().Is4() ||
		!prefix.Addr().IsPrivate() || prefix.Bits() < 16 || prefix != prefix.Masked() || !prefix.Contains(address) {
		return netip.Prefix{}, nil, errors.New("LAN requires an explicit private IPv4 address and its private subnet (/16 or narrower)")
	}
	host, authPort, err := net.SplitHostPort(authority)
	if err != nil || authPort != port || !filepath.IsAbs(cert) || !filepath.IsAbs(key) {
		return netip.Prefix{}, nil, errors.New("LAN requires an explicit HTTPS authority on the listen port and absolute TLS certificate and key paths")
	}
	if ip, err := netip.ParseAddr(host); err == nil {
		if ip != address {
			return netip.Prefix{}, nil, errors.New("HTTPS IP must match the listen IP")
		}
	} else {
		if len(host) > 253 || !strings.Contains(host, ".") {
			return netip.Prefix{}, nil, errors.New("HTTPS authority must use a qualified hostname or the listen IP")
		}
		for _, label := range strings.Split(host, ".") {
			if !dnsLabel.MatchString(label) {
				return netip.Prefix{}, nil, errors.New("invalid HTTPS hostname")
			}
		}
	}
	certKey, err := tls.LoadX509KeyPair(cert, key)
	if err != nil {
		return netip.Prefix{}, nil, errors.New("cannot load matching HTTPS certificate and key")
	}
	leaf, err := x509.ParseCertificate(certKey.Certificate[0])
	if err != nil || leaf.VerifyHostname(host) != nil {
		return netip.Prefix{}, nil, errors.New("HTTPS certificate must include the configured hostname or IP in Subject Alternative Name")
	}
	now := time.Now()
	if now.Before(leaf.NotBefore) || !now.Before(leaf.NotAfter) {
		return netip.Prefix{}, nil, errors.New("HTTPS certificate is outside its validity dates")
	}
	return prefix, &tls.Config{MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{certKey}}, nil
}

func peerIP(address string) netip.Addr {
	endpoint, _ := netip.ParseAddrPort(address)
	return endpoint.Addr().Unmap()
}

// WebRTC host candidates and the underlying UDP socket are both restricted to
// the configured LAN. Use the actual peer address, never a proxy header.
type subnetPacketConn struct {
	net.PacketConn
	lan netip.Prefix
}

func (c subnetPacketConn) ReadFrom(buffer []byte) (int, net.Addr, error) {
	for {
		n, address, err := c.PacketConn.ReadFrom(buffer)
		if err != nil || c.lan.Contains(peerIP(address.String())) {
			return n, address, err
		}
	}
}

func (c subnetPacketConn) WriteTo(buffer []byte, address net.Addr) (int, error) {
	if !c.lan.Contains(peerIP(address.String())) {
		return len(buffer), nil
	}
	return c.PacketConn.WriteTo(buffer, address)
}

type attemptWindow struct {
	expires time.Time
	count   int
}

type attemptLimiter struct {
	mu      sync.Mutex
	clients map[netip.Addr]attemptWindow
	limit   int
}

// A shared office IP can present a full cohort and a second login for each
// desktop after an outage. The Mac controller still rate-limits bad passwords
// per account. Apple sign-in has its own, stricter window.
func localAttemptLimiter(capacity int) *attemptLimiter {
	return &attemptLimiter{limit: 2*capacity + 1}
}

func (l *attemptLimiter) allow(ip netip.Addr, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.clients == nil {
		l.clients = make(map[netip.Addr]attemptWindow)
	}
	window := l.clients[ip]
	if !now.Before(window.expires) {
		if len(l.clients) >= 512 {
			for address, previous := range l.clients {
				if !now.Before(previous.expires) {
					delete(l.clients, address)
				}
			}
			if len(l.clients) >= 512 {
				return false
			}
		}
		window = attemptWindow{expires: now.Add(time.Minute)}
	}
	if window.count >= max(6, l.limit) {
		return false
	}
	window.count++
	l.clients[ip] = window
	return true
}
