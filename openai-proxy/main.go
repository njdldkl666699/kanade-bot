package main

import (
	"flag"
	"log"
	"net/http"
)

func main() {
	configPath := flag.String("config", "config.yaml", "path to YAML configuration")
	flag.Parse()
	cfg, err := loadConfig(*configPath)
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	proxy := NewProxy(cfg)
	if !cfg.Upstream.ModelSupportsImages {
		proxy.AddRequestHook(RemoveUserImagesHook{})
	}
	if len(cfg.InjectRequest) > 0 {
		proxy.AddRequestHook(InjectFieldsHook{Fields: cfg.InjectRequest})
		log.Printf("injecting missing request fields: %v", cfg.InjectRequest)
	}
	if cfg.RecordFile != "" {
		proxy.AddRequestHook(&RequestRecorderHook{Path: cfg.RecordFile})
		log.Printf("recording requests to %s", cfg.RecordFile)
	}
	server := &http.Server{Addr: cfg.Listen, Handler: proxy}
	log.Printf("openai-compatible proxy listening on %s, upstream=%s", cfg.Listen, cfg.Upstream.BaseURL)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
