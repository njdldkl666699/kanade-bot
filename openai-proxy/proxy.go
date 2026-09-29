package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// proxyState 是代理某一时刻的完整运行时状态（配置、上游 client、已注册
// hooks），以原子指针整体替换：热重载时基于新配置重建全部状态后一次性
// 换入，在途请求继续使用旧状态直至完成，不会出现半新半旧的组合。
type proxyState struct {
	cfg           Config
	client        *http.Client
	requestHooks  []RequestHook
	responseHooks []ResponseHook
}

type Proxy struct {
	state atomic.Pointer[proxyState]
}

// newUpstreamClient 构建上游 client。Timeout只约束「每次尝试等待上游响应
// 头」的阶段（见ServeHTTP：不叠加总时长上限）。不能用http.Client.Timeout
// 那种总时长上限：LLM的SSE流式响应可以合法地持续远超timeout（长生成），
// 总时长上限会在流中途掐断，触发Copilot运行时整段静默重试，造成重复生成
// 与迟到回复。流式body不设总时长，由客户端断开（如宿主会话abort）负责
// 中止上游。
func newUpstreamClient(cfg Config) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if cfg.Timeout > 0 {
		transport.ResponseHeaderTimeout = cfg.Timeout
	}
	return &http.Client{Transport: transport}
}

func NewProxy(cfg Config) *Proxy {
	p := &Proxy{}
	p.state.Store(&proxyState{cfg: cfg, client: newUpstreamClient(cfg)})
	return p
}

// Config 返回当前生效的配置（热重载后随之变化）。
func (p *Proxy) Config() Config { return p.state.Load().cfg }

// Reload 以新配置与按新配置重建的 hooks 原子替换运行时状态（client 按
// 新 timeout 重建）。在途请求不受影响，继续使用旧状态完成。
func (p *Proxy) Reload(cfg Config, requestHooks []RequestHook, responseHooks []ResponseHook) {
	p.state.Store(&proxyState{
		cfg:           cfg,
		client:        newUpstreamClient(cfg),
		requestHooks:  requestHooks,
		responseHooks: responseHooks,
	})
}

// AddRequestHook 追加请求 hook（启动阶段装配用；热重载走 Reload 整体替换）。
func (p *Proxy) AddRequestHook(h RequestHook) {
	for {
		old := p.state.Load()
		next := *old
		next.requestHooks = append(append([]RequestHook(nil), old.requestHooks...), h)
		if p.state.CompareAndSwap(old, &next) {
			return
		}
	}
}

// AddResponseHook 追加响应 hook（启动阶段装配用；热重载走 Reload 整体替换）。
func (p *Proxy) AddResponseHook(h ResponseHook) {
	for {
		old := p.state.Load()
		next := *old
		next.responseHooks = append(append([]ResponseHook(nil), old.responseHooks...), h)
		if p.state.CompareAndSwap(old, &next) {
			return
		}
	}
}

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// 接受任意 /v1/* 端点（chat/completions、responses 等），
	// 转发到 upstream.base_url + 对应子路径。
	if r.Method != http.MethodPost || !strings.HasPrefix(r.URL.Path, "/v1/") {
		http.NotFound(w, r)
		return
	}
	const maxRequestBody = 32 << 20
	body, err := io.ReadAll(io.LimitReader(r.Body, maxRequestBody+1))
	if err != nil {
		http.Error(w, "read request body", http.StatusBadRequest)
		return
	}
	if len(body) > maxRequestBody {
		http.Error(w, "request body too large", http.StatusRequestEntityTooLarge)
		return
	}
	st := p.state.Load()
	for _, hook := range st.requestHooks {
		body, err = hook.BeforeRequest(r.Context(), body, r.Header)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
	}

	// /v1/chat/completions -> base + /chat/completions；/v1/responses -> base + /responses
	subPath := strings.TrimPrefix(r.URL.Path, "/v1")
	target, err := upstreamURL(st.cfg.Upstream.BaseURL, subPath)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	// ctx直接取客户端请求上下文：客户端断开（如宿主在会话超时后abort，
	// 运行时随之断开在途请求）即取消，上游请求与重试等待随之中止。
	// 不叠加总时长超时（见newUpstreamClient注释），长流式响应不会被掐断。
	ctx := r.Context()
	upstream, err := st.forwardWithRetry(ctx, r, target, body)
	if err != nil {
		log.Printf("upstream request failed: %v", err)
		http.Error(w, "upstream request failed", http.StatusBadGateway)
		return
	}
	defer upstream.Body.Close()
	streaming := strings.Contains(strings.ToLower(upstream.Header.Get("Content-Type")), "text/event-stream")
	if len(st.responseHooks) == 0 {
		copyResponseHeaders(w.Header(), upstream.Header)
		w.WriteHeader(upstream.StatusCode)
		if streaming {
			if f, ok := w.(http.Flusher); ok {
				_, _ = io.Copy(flushWriter{w: w, f: f}, upstream.Body)
			} else {
				_, _ = io.Copy(w, upstream.Body)
			}
		} else {
			_, _ = io.Copy(w, upstream.Body)
		}
		return
	}
	responseBody, readErr := io.ReadAll(upstream.Body)
	if readErr != nil {
		http.Error(w, "read upstream response", http.StatusBadGateway)
		return
	}
	for _, hook := range st.responseHooks {
		responseBody, err = hook.AfterResponse(r.Context(), upstream.StatusCode, upstream.Header, responseBody)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadGateway)
			return
		}
	}
	copyResponseHeaders(w.Header(), upstream.Header)
	w.WriteHeader(upstream.StatusCode)
	_, _ = w.Write(responseBody)
}

type flushWriter struct {
	w io.Writer
	f http.Flusher
}

func (fw flushWriter) Write(b []byte) (int, error) {
	n, err := fw.w.Write(b)
	fw.f.Flush()
	return n, err
}

// forwardWithRetry 发送上游请求；命中 retry.statuses 的响应按 Retry-After
// （可选）或指数退避等待后原样重发，超过 max_attempts 后返回最后一次响应。
// 429/5xx 发生在响应头阶段（body 未开始转发），重试无副作用。
// 等待期间客户端断开（ctx 取消）会立即中止。
func (s *proxyState) forwardWithRetry(
	ctx context.Context, original *http.Request, target string, body []byte,
) (*http.Response, error) {
	attempt := 0
	for {
		attempt++
		upstream, err := s.doUpstream(ctx, original, target, body)
		if err != nil {
			return nil, err
		}
		if !s.retryableStatus(upstream.StatusCode) || attempt >= s.retryMaxAttempts() {
			return upstream, nil
		}
		wait := s.retryWait(upstream, attempt)
		_ = upstream.Body.Close()
		log.Printf(
			"upstream %d (attempt %d/%d), retrying in %s",
			upstream.StatusCode, attempt, s.retryMaxAttempts(), wait,
		)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(wait):
		}
	}
}

// doUpstream 构建并发送单次上游请求。body 每次重建 reader，可安全重发。
func (s *proxyState) doUpstream(
	ctx context.Context, original *http.Request, target string, body []byte,
) (*http.Response, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, target, strings.NewReader(string(body)))
	if err != nil {
		return nil, err
	}
	copyRequestHeaders(request.Header, original.Header)
	if s.cfg.Upstream.APIKey != "" {
		request.Header.Set("Authorization", "Bearer "+s.cfg.Upstream.APIKey)
	} else if incomingAuth := original.Header.Get("Authorization"); incomingAuth != "" {
		request.Header.Set("Authorization", incomingAuth)
	}
	request.Header.Set("Content-Type", "application/json")
	return s.client.Do(request)
}

// retryWait 计算下一次重试前的等待：优先 Retry-After（封顶 backoff_max），
// 未提供或解析失败时回退为指数退避（backoff_initial 翻倍，封顶 backoff_max）。
func (s *proxyState) retryWait(resp *http.Response, attempt int) time.Duration {
	initial := s.cfg.Retry.BackoffInitial
	if initial <= 0 {
		initial = 5 * time.Second
	}
	capLimit := s.cfg.Retry.BackoffMax
	if capLimit <= 0 {
		capLimit = 60 * time.Second
	}
	if respect := s.cfg.Retry.RespectRetryAfter; respect == nil || *respect {
		if d, ok := retryAfter(resp, capLimit); ok {
			return d
		}
	}
	wait := initial
	for i := 1; i < attempt && wait < capLimit; i++ {
		wait *= 2
	}
	return min(wait, capLimit)
}

// retryAfter 解析标准 Retry-After 头（延迟秒数或 HTTP-date），封顶 capLimit。
// 头不存在或无法解析时返回 ok=false。
func retryAfter(resp *http.Response, capLimit time.Duration) (time.Duration, bool) {
	v := strings.TrimSpace(resp.Header.Get("Retry-After"))
	if v == "" {
		return 0, false
	}
	if secs, err := strconv.ParseFloat(v, 64); err == nil && secs >= 0 {
		return min(time.Duration(secs*float64(time.Second)), capLimit), true
	}
	if at, err := http.ParseTime(v); err == nil {
		return min(max(time.Until(at), 0), capLimit), true
	}
	return 0, false
}

func (s *proxyState) retryableStatus(code int) bool {
	return slices.Contains(s.cfg.Retry.Statuses, code)
}

func (s *proxyState) retryMaxAttempts() int {
	if s.cfg.Retry.MaxAttempts > 0 {
		return s.cfg.Retry.MaxAttempts
	}
	return 1
}

func upstreamURL(base, path string) (string, error) {
	u, err := url.Parse(strings.TrimRight(base, "/") + "/")
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "", fmt.Errorf("invalid upstream.base_url")
	}
	u.Path = strings.TrimRight(u.Path, "/") + path
	return u.String(), nil
}

func copyRequestHeaders(dst, src http.Header) {
	for key, values := range src {
		if strings.EqualFold(key, "Authorization") || strings.EqualFold(key, "Host") || strings.EqualFold(key, "Content-Length") {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}

func copyResponseHeaders(dst, src http.Header) {
	for key, values := range src {
		if strings.EqualFold(key, "Content-Length") || strings.EqualFold(key, "Transfer-Encoding") || strings.EqualFold(key, "Connection") {
			continue
		}
		for _, value := range values {
			dst.Add(key, value)
		}
	}
}
