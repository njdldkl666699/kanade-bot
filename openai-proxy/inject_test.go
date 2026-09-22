package main

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"
)

func TestInjectFieldsHook(t *testing.T) {
	hook := InjectFieldsHook{Fields: map[string]any{"max_tokens": 4096, "temperature": 0.2}}

	// 缺失字段被注入，已有字段（temperature=1）不被覆盖。
	body := []byte(`{"model":"deepseek-flash","temperature":1,"messages":[]}`)
	out, err := hook.BeforeRequest(context.Background(), body, http.Header{})
	if err != nil {
		t.Fatal(err)
	}
	var root map[string]any
	if err := json.Unmarshal(out, &root); err != nil {
		t.Fatal(err)
	}
	if root["max_tokens"] != float64(4096) {
		t.Fatalf("max_tokens not injected: %v", root)
	}
	if root["temperature"] != float64(1) {
		t.Fatalf("existing temperature was overwritten: %v", root)
	}
}

func TestInjectFieldsHookNoChange(t *testing.T) {
	hook := InjectFieldsHook{Fields: map[string]any{"max_tokens": 4096}}
	body := []byte(`{"max_tokens":16,"messages":[]}`)
	out, err := hook.BeforeRequest(context.Background(), body, http.Header{})
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != string(body) {
		t.Fatalf("body should be untouched: %s", out)
	}
}

func TestInjectFieldsHookEmpty(t *testing.T) {
	hook := InjectFieldsHook{}
	body := []byte(`{"messages":[]}`)
	out, err := hook.BeforeRequest(context.Background(), body, http.Header{})
	if err != nil {
		t.Fatal(err)
	}
	if string(out) != string(body) {
		t.Fatalf("body should be untouched: %s", out)
	}
}
