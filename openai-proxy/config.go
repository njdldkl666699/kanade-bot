package main

import (
	"fmt"
	"os"
	"time"

	"gopkg.in/yaml.v3"
)

type Config struct {
	Listen   string         `yaml:"listen"`
	Upstream UpstreamConfig `yaml:"upstream"`
	Timeout  time.Duration  `yaml:"timeout"`
	// RecordFile 追加记录每个上游请求（JSONL）。为空时不记录。
	RecordFile string `yaml:"record_file"`
	// InjectRequest 仅在请求体顶层缺失时注入的字段（如 max_tokens: 4096）。
	// Copilot 运行时不会把 BYOK 配置的 max_output_tokens 写入上游请求体，
	// 可用此机制补齐。
	InjectRequest map[string]any `yaml:"inject_request"`
}

type UpstreamConfig struct {
	BaseURL             string `yaml:"base_url"`
	APIKey              string `yaml:"api_key"`
	ModelSupportsImages bool   `yaml:"model_supports_images"`
}

func loadConfig(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return Config{}, fmt.Errorf("parse config: %w", err)
	}
	if cfg.Listen == "" {
		cfg.Listen = ":8080"
	}
	if cfg.Timeout == 0 {
		cfg.Timeout = 5 * time.Minute
	}
	if cfg.Upstream.BaseURL == "" {
		return Config{}, fmt.Errorf("upstream.base_url is required")
	}
	return cfg, nil
}
