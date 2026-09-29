package main

// watch 基于 viper（fsnotify）监听配置文件变更：文件被直接写入或被编辑器
// 以「临时文件写入 + rename」方式原子替换时触发重载。变更检测交给 viper，
// 解析始终走与启动时同一份 loadConfig（yaml.v3），保证热重载与重启加载
// 的语义完全一致；viper 自身的解析结果不参与装配。

import (
	"log"
	"time"

	"github.com/fsnotify/fsnotify"
	"github.com/spf13/viper"
)

// configReloadDebounce 事件防抖窗口：编辑器保存常产生连续多次文件事件
// （写入、chmod、rename 等），等文件静止后再统一重载，也降低读到
// 半截文件的概率。
const configReloadDebounce = 200 * time.Millisecond

// 半截文件兜底：防抖后仍可能读到写入中的文件，且此后未必再有新事件，
// 解析失败时按固定间隔重试有限次，仍失败则放弃（保留旧配置），
// 等待下一次文件变更。
const (
	configReloadRetryDelay = 500 * time.Millisecond
	configReloadRetries    = 3
)

// watchConfig 监听 path 并在变更时回调 reload（传入重新解析后的配置）。
// 解析失败（如文件正被写到一半）时记录日志并保留旧配置，等待下一次
// 事件再试。reload 在单个 goroutine 中串行执行。
func watchConfig(path string, reload func(Config)) {
	v := viper.New()
	v.SetConfigFile(path)
	if err := v.ReadInConfig(); err != nil {
		log.Printf("watch config %s: %v", path, err)
	}
	events := make(chan struct{}, 1)
	v.OnConfigChange(func(fsnotify.Event) {
		select { // 非阻塞投递：堆积的事件合并为一次重载
		case events <- struct{}{}:
		default:
		}
	})
	v.WatchConfig()
	go func() {
		var timer <-chan time.Time // nil：当前无待处理动作
		failed := 0                // 连续解析失败次数
		for {
			select {
			case <-events:
				// 每次事件重置防抖计时并清零失败计数；被替换掉的旧
				// timer 未触发即作废。
				timer = time.After(configReloadDebounce)
				failed = 0
			case <-timer:
				cfg, err := loadConfig(path)
				if err == nil {
					timer = nil
					reload(cfg)
					continue
				}
				log.Printf("config reload failed (keeping old config): %v", err)
				if failed < configReloadRetries {
					failed++
					timer = time.After(configReloadRetryDelay)
					continue
				}
				timer = nil // 文件持续损坏：放弃，等下一次变更事件
			}
		}
	}()
}
