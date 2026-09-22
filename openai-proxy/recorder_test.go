package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRequestRecorderHook(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "capture.jsonl")
	hook := &RequestRecorderHook{Path: path}

	body := []byte(`{"model":"deepseek-flash","max_tokens":2345}`)
	header := http.Header{}
	header.Set("Authorization", "Bearer sk-secret")
	header.Set("Content-Type", "application/json")

	out, err := hook.BeforeRequest(context.Background(), body, header)
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != string(body) {
		t.Fatalf("recorder must not modify the body: %s", out)
	}
	// 第二次写入验证追加行为。
	if _, err := hook.BeforeRequest(context.Background(), body, header); err != nil {
		t.Fatal(err)
	}

	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 recorded lines, got %d", len(lines))
	}
	var entry struct {
		TS      string            `json:"ts"`
		Headers map[string]string `json:"headers"`
		Body    json.RawMessage   `json:"body"`
	}
	if err := json.Unmarshal([]byte(lines[0]), &entry); err != nil {
		t.Fatal(err)
	}
	if entry.TS == "" {
		t.Fatal("missing timestamp")
	}
	if entry.Headers["Authorization"] != "<redacted>" {
		t.Fatalf("Authorization header was not redacted: %v", entry.Headers)
	}
	if !strings.Contains(string(entry.Body), `"max_tokens":2345`) {
		t.Fatalf("body was not preserved verbatim: %s", entry.Body)
	}
}

func TestRequestRecorderHookInvalidJSON(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "capture.jsonl")
	hook := &RequestRecorderHook{Path: path}

	if _, err := hook.BeforeRequest(context.Background(), []byte("not json"), http.Header{}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var entry struct {
		BodyRaw string `json:"body_raw"`
	}
	if err := json.Unmarshal(data, &entry); err != nil {
		t.Fatal(err)
	}
	if entry.BodyRaw != "not json" {
		t.Fatalf("raw body not preserved: %q", entry.BodyRaw)
	}
}
