package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
)

// RequestHook runs before a request is sent upstream. It may return a
// replacement JSON body. Headers are provided for hooks that need context.
type RequestHook interface {
	BeforeRequest(context.Context, []byte, http.Header) ([]byte, error)
}

// InjectFieldsHook injects top-level fields into the request body when they
// are missing. Fields already present in the body are left untouched.
type InjectFieldsHook struct {
	Fields map[string]any
}

func (h InjectFieldsHook) BeforeRequest(_ context.Context, body []byte, _ http.Header) ([]byte, error) {
	if len(h.Fields) == 0 {
		return body, nil
	}
	var root map[string]any
	if err := json.Unmarshal(body, &root); err != nil {
		return nil, fmt.Errorf("decode request body: %w", err)
	}
	changed := false
	for key, value := range h.Fields {
		if _, exists := root[key]; !exists {
			root[key] = value
			changed = true
		}
	}
	if !changed {
		return body, nil
	}
	out, err := json.Marshal(root)
	if err != nil {
		return nil, fmt.Errorf("encode chat completion request: %w", err)
	}
	return out, nil
}

// ResponseHook runs after an upstream response has been fully read. It may
// return a replacement response body. Streaming responses are buffered only
// when at least one response hook is installed.
type ResponseHook interface {
	AfterResponse(context.Context, int, http.Header, []byte) ([]byte, error)
}

// RemoveUserImagesHook removes image content blocks from user messages.
type RemoveUserImagesHook struct{}

func (RemoveUserImagesHook) BeforeRequest(_ context.Context, body []byte, _ http.Header) ([]byte, error) {
	var root map[string]any
	if err := json.Unmarshal(body, &root); err != nil {
		return nil, fmt.Errorf("decode request body: %w", err)
	}
	messages, ok := root["messages"].([]any)
	if !ok {
		return body, nil
	}
	changed := false
	for _, raw := range messages {
		message, ok := raw.(map[string]any)
		if !ok || message["role"] != "user" {
			continue
		}
		parts, ok := message["content"].([]any)
		if !ok {
			continue
		}
		filtered := make([]any, 0, len(parts))
		for _, part := range parts {
			obj, isObject := part.(map[string]any)
			if isObject {
				typeName, _ := obj["type"].(string)
				if typeName == "image_url" || typeName == "input_image" {
					changed = true
					continue
				}
			}
			filtered = append(filtered, part)
		}
		message["content"] = filtered
	}
	if !changed {
		return body, nil
	}
	var out bytes.Buffer
	if err := json.NewEncoder(&out).Encode(root); err != nil {
		return nil, fmt.Errorf("encode chat completion request: %w", err)
	}
	return bytes.TrimSpace(out.Bytes()), nil
}
