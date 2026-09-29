package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestProxyReloadSwapsUpstreamAndHooks(t *testing.T) {
	// 两个上游分别返回不同标记，并记录收到的请求体。
	var mu sync.Mutex
	lastBody := map[string]string{}
	newUpstream := func(name string) *httptest.Server {
		return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			body, _ := io.ReadAll(r.Body)
			mu.Lock()
			lastBody[name] = string(body)
			mu.Unlock()
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"up":"` + name + `"}`))
		}))
	}
	upstreamA, upstreamB := newUpstream("a"), newUpstream("b")
	defer upstreamA.Close()
	defer upstreamB.Close()

	do := func(p *Proxy) string {
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"messages":[]}`))
		rec := httptest.NewRecorder()
		p.ServeHTTP(rec, req)
		return rec.Body.String()
	}

	p := NewProxy(Config{Upstream: UpstreamConfig{BaseURL: upstreamA.URL + "/v1"}})
	p.AddRequestHook(InjectFieldsHook{Fields: map[string]any{"marker": "old"}})
	if got := do(p); got != `{"up":"a"}` {
		t.Fatalf("initial upstream: %s", got)
	}
	mu.Lock()
	body := lastBody["a"]
	mu.Unlock()
	if !strings.Contains(body, `"marker":"old"`) {
		t.Fatalf("initial request hook not applied: %s", body)
	}

	// 热重载：切换上游并整体替换 hooks（不再注入 marker）。
	cfgB := Config{Upstream: UpstreamConfig{BaseURL: upstreamB.URL + "/v1"}}
	p.Reload(cfgB, nil, nil)
	if got := do(p); got != `{"up":"b"}` {
		t.Fatalf("upstream after reload: %s", got)
	}
	mu.Lock()
	body = lastBody["b"]
	mu.Unlock()
	if strings.Contains(body, "marker") {
		t.Fatalf("hook should be replaced on reload: %s", body)
	}
	if p.Config().Upstream.BaseURL != cfgB.Upstream.BaseURL {
		t.Fatalf("Config() = %+v", p.Config())
	}
}

func TestWatchConfigReloads(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	write := func(content string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("upstream:\n  base_url: http://first\n")
	reloaded := make(chan Config, 8)
	watchConfig(path, func(cfg Config) { reloaded <- cfg })
	waitFor := func(wantBaseURL, how string) {
		t.Helper()
		deadline := time.Now().Add(8 * time.Second)
		for {
			select {
			case cfg := <-reloaded:
				if cfg.Upstream.BaseURL == wantBaseURL {
					return
				}
			case <-time.After(time.Until(deadline)):
				t.Fatalf("no reload to %s after %s", wantBaseURL, how)
			}
		}
	}

	// 直接覆盖写入。
	write("upstream:\n  base_url: http://second\n")
	waitFor("http://second", "direct write")

	// 编辑器式保存：写临时文件后 rename 原子替换。
	tmp := filepath.Join(dir, "config.yaml.tmp")
	if err := os.WriteFile(tmp, []byte("upstream:\n  base_url: http://third\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, path); err != nil {
		t.Fatal(err)
	}
	waitFor("http://third", "rename replace")
}

func TestDiffConfigs(t *testing.T) {
	oldCfg := Config{
		Listen:  ":8080",
		Timeout: 5 * time.Minute,
		Upstream: UpstreamConfig{
			BaseURL: "http://a", APIKey: "sk-old", ModelSupportsImages: true,
		},
		Retry:             RetryConfig{Statuses: []int{429}, MaxAttempts: 3},
		FixReasoningFinal: true,
	}
	newCfg := oldCfg
	newCfg.Listen = ":9090"
	newCfg.Upstream.BaseURL = "http://b"
	newCfg.Upstream.APIKey = "sk-new"
	newCfg.Upstream.ModelSupportsImages = false
	newCfg.Retry = RetryConfig{Statuses: []int{429, 503}, MaxAttempts: 5}
	newCfg.FixReasoningFinal = false

	diffs := strings.Join(diffConfigs(oldCfg, newCfg), "\n")
	for _, want := range []string{
		"listen: :8080 -> :9090 (requires restart)",
		"upstream.base_url: http://a -> http://b",
		"upstream.api_key changed",
		"upstream.model_supports_images: true -> false",
		"respect_retry_after:true",
		"fix_reasoning_final: true -> false",
	} {
		if !strings.Contains(diffs, want) {
			t.Fatalf("diff missing %q in:\n%s", want, diffs)
		}
	}
	// 密钥值不得出现在日志里。
	if strings.Contains(diffs, "sk-old") || strings.Contains(diffs, "sk-new") {
		t.Fatalf("api key leaked into diff:\n%s", diffs)
	}
	// 无变化时输出为空。
	if got := diffConfigs(oldCfg, oldCfg); len(got) != 0 {
		t.Fatalf("identical configs produced diffs: %v", got)
	}
}
