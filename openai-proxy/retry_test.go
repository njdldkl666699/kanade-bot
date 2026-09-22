package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestProxyRetriesOn429ThenSucceeds(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		body, _ := io.ReadAll(r.Body)
		if string(body) != `{"model":"x"}` {
			t.Errorf("body = %s", body)
		}
		if calls < 3 {
			w.WriteHeader(http.StatusTooManyRequests)
			_, _ = w.Write([]byte(`{"error":"tpm exhausted"}`))
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	p := NewProxy(Config{
		Upstream: UpstreamConfig{BaseURL: upstream.URL + "/v1"},
		Retry: RetryConfig{
			Statuses:       []int{http.StatusTooManyRequests},
			MaxAttempts:    3,
			BackoffInitial: 1 * time.Millisecond,
			BackoffMax:     4 * time.Millisecond,
		},
	})
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"x"}`))
	rec := httptest.NewRecorder()
	p.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || rec.Body.String() != `{"ok":true}` {
		t.Fatalf("response: %d %s", rec.Code, rec.Body.String())
	}
	if calls != 3 {
		t.Fatalf("upstream calls = %d, want 3", calls)
	}
}

func TestProxyRetryExhaustedReturnsLastResponse(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":"tpm exhausted"}`))
	}))
	defer upstream.Close()

	p := NewProxy(Config{
		Upstream: UpstreamConfig{BaseURL: upstream.URL + "/v1"},
		Retry: RetryConfig{
			Statuses:       []int{http.StatusTooManyRequests},
			MaxAttempts:    2,
			BackoffInitial: 1 * time.Millisecond,
		},
	})
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	p.ServeHTTP(rec, req)
	// 重试耗尽后原样返回最后一次上游响应，不吞错误
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("status = %d, want 429", rec.Code)
	}
	if calls != 2 {
		t.Fatalf("upstream calls = %d, want 2", calls)
	}
}

func TestProxyNoRetryWhenNotConfigured(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	defer upstream.Close()

	p := NewProxy(Config{Upstream: UpstreamConfig{BaseURL: upstream.URL + "/v1"}})
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{}`))
	rec := httptest.NewRecorder()
	p.ServeHTTP(rec, req)
	if rec.Code != http.StatusTooManyRequests || calls != 1 {
		t.Fatalf("status = %d calls = %d, want 429/1（未配置重试时保持原行为）", rec.Code, calls)
	}
}

func TestRetryWaitExponentialBackoff(t *testing.T) {
	p := &Proxy{cfg: Config{Retry: RetryConfig{
		BackoffInitial: 2 * time.Second,
		BackoffMax:     8 * time.Second,
	}}}
	cases := []struct {
		attempt int
		want    time.Duration
	}{
		{1, 2 * time.Second},
		{2, 4 * time.Second},
		{3, 8 * time.Second},
		{4, 8 * time.Second}, // 封顶
		{10, 8 * time.Second},
	}
	for _, c := range cases {
		resp := &http.Response{Header: http.Header{}}
		if got := p.retryWait(resp, c.attempt); got != c.want {
			t.Fatalf("retryWait(attempt=%d) = %s, want %s", c.attempt, got, c.want)
		}
	}
}

func TestRetryWaitPrefersRetryAfterHeader(t *testing.T) {
	p := &Proxy{cfg: Config{Retry: RetryConfig{
		BackoffInitial: 30 * time.Second,
		BackoffMax:     60 * time.Second,
	}}}

	// 秒数形式，优先于指数退避
	resp := &http.Response{Header: http.Header{"Retry-After": []string{"3"}}}
	if got := p.retryWait(resp, 1); got != 3*time.Second {
		t.Fatalf("retryWait = %s, want 3s", got)
	}
	// 超过 backoff_max 时封顶
	resp = &http.Response{Header: http.Header{"Retry-After": []string{"120"}}}
	if got := p.retryWait(resp, 1); got != 60*time.Second {
		t.Fatalf("retryWait = %s, want 60s（封顶）", got)
	}
	// 小数秒
	resp = &http.Response{Header: http.Header{"Retry-After": []string{"1.5"}}}
	if got := p.retryWait(resp, 1); got != 1500*time.Millisecond {
		t.Fatalf("retryWait = %s, want 1.5s", got)
	}
	// 非法值回退到指数退避
	resp = &http.Response{Header: http.Header{"Retry-After": []string{"soon"}}}
	if got := p.retryWait(resp, 1); got != 30*time.Second {
		t.Fatalf("retryWait = %s, want 30s（回退指数退避）", got)
	}
	// 显式关闭 respect_retry_after 后忽略该头
	disable := false
	p2 := &Proxy{cfg: Config{Retry: RetryConfig{
		BackoffInitial:    30 * time.Second,
		RespectRetryAfter: &disable,
	}}}
	resp = &http.Response{Header: http.Header{"Retry-After": []string{"3"}}}
	if got := p2.retryWait(resp, 1); got != 30*time.Second {
		t.Fatalf("retryWait = %s, want 30s（respect_retry_after=false）", got)
	}
}

func TestRetryAfterHTTPDate(t *testing.T) {
	max := 60 * time.Second
	// HTTP-date 形式
	future := time.Now().Add(10 * time.Second).UTC().Format(http.TimeFormat)
	resp := &http.Response{Header: http.Header{"Retry-After": []string{future}}}
	d, ok := retryAfter(resp, max)
	if !ok || d <= 0 || d > max {
		t.Fatalf("retryAfter(http-date) = %s ok=%v", d, ok)
	}
	// 过去的时间戳 → 0
	past := time.Now().Add(-time.Hour).UTC().Format(http.TimeFormat)
	resp = &http.Response{Header: http.Header{"Retry-After": []string{past}}}
	if d, ok := retryAfter(resp, max); !ok || d != 0 {
		t.Fatalf("retryAfter(past) = %s ok=%v, want 0", d, ok)
	}
}
