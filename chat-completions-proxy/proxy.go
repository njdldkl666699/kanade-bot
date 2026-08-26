package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
)

type Proxy struct {
	cfg           Config
	client        *http.Client
	requestHooks  []RequestHook
	responseHooks []ResponseHook
}

func NewProxy(cfg Config) *Proxy {
	return &Proxy{cfg: cfg, client: &http.Client{Timeout: cfg.Timeout}}
}

func (p *Proxy) AddRequestHook(h RequestHook)   { p.requestHooks = append(p.requestHooks, h) }
func (p *Proxy) AddResponseHook(h ResponseHook) { p.responseHooks = append(p.responseHooks, h) }

func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost || r.URL.Path != "/v1/chat/completions" {
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
	for _, hook := range p.requestHooks {
		body, err = hook.BeforeRequest(r.Context(), body, r.Header)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
	}

	target, err := upstreamURL(p.cfg.Upstream.BaseURL, "/chat/completions")
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	ctx := r.Context()
	if p.cfg.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, p.cfg.Timeout)
		defer cancel()
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, target, strings.NewReader(string(body)))
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	copyRequestHeaders(request.Header, r.Header)
	if p.cfg.Upstream.APIKey != "" {
		request.Header.Set("Authorization", "Bearer "+p.cfg.Upstream.APIKey)
	} else if incomingAuth := r.Header.Get("Authorization"); incomingAuth != "" {
		request.Header.Set("Authorization", incomingAuth)
	}
	request.Header.Set("Content-Type", "application/json")

	upstream, err := p.client.Do(request)
	if err != nil {
		log.Printf("upstream request failed: %v", err)
		http.Error(w, "upstream request failed", http.StatusBadGateway)
		return
	}
	defer upstream.Body.Close()
	streaming := strings.Contains(strings.ToLower(upstream.Header.Get("Content-Type")), "text/event-stream")
	if len(p.responseHooks) == 0 {
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
	for _, hook := range p.responseHooks {
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
