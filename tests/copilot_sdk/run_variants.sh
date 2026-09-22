#!/usr/bin/env bash
# 运行 Copilot SDK max_output_tokens 抓包测试。
# 用法: bash tests/copilot_sdk/run_variants.sh [ONLY 列表]
#   bash tests/copilot_sdk/run_variants.sh                    # 全量运行（清空历史捕获）
#   bash tests/copilot_sdk/run_variants.sh provider,named_models # 只运行指定变体
set -euo pipefail
cd "$(dirname "$0")/../.."
exec env ONLY="${1:-}" .venv/bin/python tests/copilot_sdk/test_max_output_tokens.py
