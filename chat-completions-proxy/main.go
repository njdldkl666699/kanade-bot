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
	server := &http.Server{Addr: cfg.Listen, Handler: proxy}
	log.Printf("chat completions proxy listening on %s, upstream=%s", cfg.Listen, cfg.Upstream.BaseURL)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
