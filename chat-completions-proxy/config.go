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
