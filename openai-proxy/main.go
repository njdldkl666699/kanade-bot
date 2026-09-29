package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"reflect"
)

func main() {
	configPath := flag.String("config", "config.yaml", "path to YAML configuration")
	flag.Parse()
	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	proxy := NewProxy(cfg)
	requestHooks, responseHooks := wireHooks(cfg)
	for _, hook := range requestHooks {
		proxy.AddRequestHook(hook)
	}
	for _, hook := range responseHooks {
		proxy.AddResponseHook(hook)
	}
	// 配置热重载：viper 监听文件变更（防抖），重新解析后原子替换代理的
	// 运行时状态（配置、client、hooks），无需重启即可切换上游/参数。
	// listen 例外：监听地址已随 server 绑定，变更需重启（日志会提示）。
	watchConfig(*configPath, func(newCfg Config) {
		for _, change := range diffConfigs(proxy.Config(), newCfg) {
			log.Printf("config change: %s", change)
		}
		requestHooks, responseHooks := wireHooks(newCfg)
		proxy.Reload(newCfg, requestHooks, responseHooks)
		log.Printf("config reloaded: %s", *configPath)
	})
	server := &http.Server{Addr: cfg.Listen, Handler: proxy}
	log.Printf("openai-compatible proxy listening on %s, upstream=%s", cfg.Listen, cfg.Upstream.BaseURL)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}

// wireHooks 根据配置构造请求/响应 hooks。启动装配与热重载共用同一份逻辑，
// 保证重载后的行为与「用新配置重启」完全一致。
func wireHooks(cfg Config) (requestHooks []RequestHook, responseHooks []ResponseHook) {
	if !cfg.Upstream.ModelSupportsImages {
		requestHooks = append(requestHooks, RemoveUserImagesHook{})
	}
	if len(cfg.InjectRequest) > 0 {
		requestHooks = append(requestHooks, InjectFieldsHook{Fields: cfg.InjectRequest})
		log.Printf("injecting missing request fields: %v", cfg.InjectRequest)
	}
	if cfg.RecordFile != "" {
		requestHooks = append(requestHooks, &RequestRecorderHook{Path: cfg.RecordFile})
		log.Printf("recording requests to %s", cfg.RecordFile)
	}
	return requestHooks, responseHooks
}

// diffConfigs 列出两次配置的关键差异，用于热重载日志。api_key 只报告是否
// 变化，不打印值；listen 的差异随重启提示一并展示。
func diffConfigs(oldCfg, newCfg Config) []string {
	var diffs []string
	add := func(format string, args ...any) {
		diffs = append(diffs, fmt.Sprintf(format, args...))
	}
	if oldCfg.Listen != newCfg.Listen {
		add("listen: %s -> %s (requires restart)", oldCfg.Listen, newCfg.Listen)
	}
	if oldCfg.Upstream.BaseURL != newCfg.Upstream.BaseURL {
		add("upstream.base_url: %s -> %s", oldCfg.Upstream.BaseURL, newCfg.Upstream.BaseURL)
	}
	if oldCfg.Upstream.APIKey != newCfg.Upstream.APIKey {
		add("upstream.api_key changed")
	}
	if oldCfg.Upstream.ModelSupportsImages != newCfg.Upstream.ModelSupportsImages {
		add("upstream.model_supports_images: %t -> %t",
			oldCfg.Upstream.ModelSupportsImages, newCfg.Upstream.ModelSupportsImages)
	}
	if oldCfg.Timeout != newCfg.Timeout {
		add("timeout: %s -> %s", oldCfg.Timeout, newCfg.Timeout)
	}
	if oldCfg.RecordFile != newCfg.RecordFile {
		add("record_file: %q -> %q", oldCfg.RecordFile, newCfg.RecordFile)
	}
	if !reflect.DeepEqual(oldCfg.InjectRequest, newCfg.InjectRequest) {
		add("inject_request: %v -> %v", oldCfg.InjectRequest, newCfg.InjectRequest)
	}
	if !reflect.DeepEqual(oldCfg.Retry, newCfg.Retry) {
		add("retry: %s -> %s", retrySummary(oldCfg.Retry), retrySummary(newCfg.Retry))
	}
	return diffs
}

// retrySummary 生成 retry 配置的可读摘要（解引用指针，避免打印地址）。
func retrySummary(r RetryConfig) string {
	respect := "true" // 默认值
	if r.RespectRetryAfter != nil {
		respect = fmt.Sprintf("%t", *r.RespectRetryAfter)
	}
	return fmt.Sprintf("{statuses:%v max_attempts:%d backoff_initial:%s backoff_max:%s respect_retry_after:%s}",
		r.Statuses, r.MaxAttempts, r.BackoffInitial, r.BackoffMax, respect)
}
