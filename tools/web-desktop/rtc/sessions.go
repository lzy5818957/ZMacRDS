package main

import (
	"encoding/json"
	"net"
	"os"
	"slices"
	"sync"
	"time"
)

type browserSession struct {
	Username    string `json:"username"`
	ConnectedAt int64  `json:"connected_at"`
}

type sessionList struct {
	mu      sync.Mutex
	current map[*browserSession]struct{}
}

func (s *sessionList) add(username string) func() {
	row := &browserSession{Username: username, ConnectedAt: time.Now().Unix()}
	s.mu.Lock()
	if s.current == nil {
		s.current = make(map[*browserSession]struct{})
	}
	s.current[row] = struct{}{}
	s.mu.Unlock()
	return func() {
		s.mu.Lock()
		delete(s.current, row)
		s.mu.Unlock()
	}
}

func (s *sessionList) snapshot() []browserSession {
	s.mu.Lock()
	defer s.mu.Unlock()
	rows := make([]browserSession, 0, len(s.current))
	for session := range s.current {
		rows = append(rows, *session)
	}
	slices.SortFunc(rows, func(a, b browserSession) int {
		switch {
		case a.ConnectedAt < b.ConnectedAt:
			return -1
		case a.ConnectedAt > b.ConnectedAt:
			return 1
		default:
			return 0
		}
	})
	return rows
}

// Readable only by the dedicated web worker and the root controller.
// No session mutation or credentials are exposed on this endpoint.
func (s *sessionList) listen(path string) (net.Listener, error) {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	if err := os.Chmod(path, 0600); err != nil {
		_ = listener.Close()
		return nil, err
	}
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			_ = conn.SetWriteDeadline(time.Now().Add(250 * time.Millisecond))
			_ = json.NewEncoder(conn).Encode(s.snapshot())
			_ = conn.Close()
		}
	}()
	return listener, nil
}
