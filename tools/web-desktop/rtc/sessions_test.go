package main

import (
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"
)

func TestManagementSocketReportsLiveBrowserSessions(t *testing.T) {
	// Darwin limits Unix-domain socket addresses to roughly 100 bytes.
	dir, err := os.MkdirTemp("/tmp", "zmacrds-web-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "status.sock")
	var sessions sessionList
	listener, err := sessions.listen(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	if info, err := os.Stat(path); err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("expected private status socket, got %v, %v", info, err)
	}
	read := func(want map[string]int) {
		t.Helper()
		conn, err := net.DialTimeout("unix", path, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		defer conn.Close()
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		var rows []browserSession
		decoder := json.NewDecoder(conn)
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&rows); err != nil || rows == nil {
			t.Fatalf("expected a JSON session array, got %v, %v", rows, err)
		}
		got := make(map[string]int)
		for _, row := range rows {
			if row.ConnectedAt <= 0 {
				t.Fatal("missing connection time")
			}
			got[row.Username]++
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
	read(map[string]int{})
	endFirst := sessions.add("first")
	endSecond := sessions.add("second")
	read(map[string]int{"first": 1, "second": 1})
	endFirst()
	read(map[string]int{"second": 1})
	endSecond()
	read(map[string]int{})

	// At the highest configured capacity, the JSON report exceeds the old
	// 4 KiB management read limit. Every connected account must still appear.
	want := make(map[string]int, 64)
	for i := 0; i < 64; i++ {
		name := fmt.Sprintf("qa-%061d", i)
		want[name] = 1
		t.Cleanup(sessions.add(name))
	}
	read(want)
}
