package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sync"
	"time"
)

// RequestRecorderHook appends every upstream request to a JSONL file without
// modifying it. Each line is one JSON object:
//
//	{"ts":"...","url":"...","headers":{...},"body":{...}}
//
// Sensitive headers (Authorization, Cookie) are replaced with a placeholder so
// the capture file can be shared safely. Bodies that fail to parse as JSON are
// kept verbatim in the "body_raw" field.
type RequestRecorderHook struct {
	Path string

	mu sync.Mutex
}

func (h *RequestRecorderHook) BeforeRequest(ctx context.Context, body []byte, header http.Header) ([]byte, error) {
	entry := map[string]any{
		"ts":      time.Now().Format(time.RFC3339Nano),
		"headers": sanitizedHeaders(header),
	}
	if json.Valid(body) {
		entry["body"] = json.RawMessage(body)
	} else {
		entry["body_raw"] = string(body)
	}
	line, err := json.Marshal(entry)
	if err != nil {
		return nil, fmt.Errorf("encode recorded request: %w", err)
	}

	h.mu.Lock()
	defer h.mu.Unlock()
	f, err := os.OpenFile(h.Path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("open record file %s: %w", h.Path, err)
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return nil, fmt.Errorf("append record file %s: %w", h.Path, err)
	}
	log.Printf("recorded request (%d bytes) to %s", len(body), h.Path)
	// 返回原始 body，不修改请求。
	return body, nil
}

func sanitizedHeaders(header http.Header) map[string]string {
	out := make(map[string]string, len(header))
	for key, values := range header {
		switch key {
		case "Authorization", "Cookie", "Proxy-Authorization":
			out[key] = "<redacted>"
		default:
			out[key] = values[0]
		}
	}
	return out
}
