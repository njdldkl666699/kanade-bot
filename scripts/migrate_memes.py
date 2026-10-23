#!/usr/bin/env python3
"""存量表情包迁移：旧类别目录 + 人工描述 → 视觉打标 + 向量索引

前置条件（配置文件）：

- `chat.sticker_meme.tagger`：视觉打标模型
- `chat.sticker_meme.embedding.model`：embedding 模型（OpenAI 兼容端点）

用法：
    uv run -m scripts.migrate_memes [--dry-run] [--apply] [--concurrency 3]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionContentPartParam

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 按文件路径加载纯逻辑模块：chat 包的 __init__ 依赖已初始化的 nonebot，
# 独立脚本无法直接 `import kanade_bot.plugins.chat.meme_index`
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "kanade_bot.plugins.chat.meme_index",
    PROJECT_ROOT / "kanade_bot" / "plugins" / "chat" / "meme_index.py",
)
assert _spec is not None and _spec.loader is not None
meme_index = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = meme_index
_spec.loader.exec_module(meme_index)

from kanade_bot.plugins.chat.meme_index import (
    IMAGE_EXTS,
    TAGGING_FALLBACK_PROMPT,
    EmbeddingClient,
    MemesIndex,
    parse_model_tags,
)
from scripts.util import load_configs

CHAT_CONFIGS_PATH = PROJECT_ROOT / "config" / "chat" / "chat_configs.json"
TAGGING_PROMPT_PATH = PROJECT_ROOT / "config" / "chat" / "StickerTagging.md"


def _load_tagging_prompt() -> str:
    if TAGGING_PROMPT_PATH.is_file():
        return TAGGING_PROMPT_PATH.read_text("utf-8").strip() or TAGGING_FALLBACK_PROMPT
    return TAGGING_FALLBACK_PROMPT


async def _tag_image(
    client: AsyncOpenAI, model: str, prompt: str, data: bytes, mime_type: str
) -> list[str]:
    """调用视觉模型打标，失败重试一次；始终失败返回空列表"""
    b64 = base64.b64encode(data).decode()
    content: list[ChatCompletionContentPartParam] = [
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        {"type": "text", "text": "请为这张表情包生成标签。"},
    ]
    for _ in range(2):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": content},
                ],
            )
            if tags := parse_model_tags(resp.choices[0].message.content or ""):
                return tags
            print(f"    打标输出无法解析：{(resp.choices[0].message.content or '')[:120]}")
        except Exception as e:
            print(f"    打标请求失败：{e}")
    return []


async def main() -> int:
    parser = argparse.ArgumentParser(description="存量表情包迁移")
    parser.add_argument("--dry-run", action="store_true", help="只打标打印结果，不写索引不动文件")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="迁移完成后从 chat_configs.json 移除 memes 字段并清理旧目录",
    )
    parser.add_argument("--concurrency", type=int, default=3, help="视觉打标并发数")
    args = parser.parse_args()

    _, configs = load_configs(PROJECT_ROOT)
    chat_cfg: dict = configs.get("chat") or {}

    # 打标模型配置（chat.sticker_meme.tagger）
    sm: dict = chat_cfg.get("sticker_meme") or {}
    tagger: dict = sm.get("tagger") or {}
    if not tagger.get("model"):
        print("错误：配置文件中未配置 chat.sticker_meme.tagger（视觉打标模型），请先配置")
        return 1
    tag_provider: dict = tagger.get("provider") or {}
    tag_client = AsyncOpenAI(
        base_url=tag_provider.get("base_url"),
        api_key=tag_provider.get("api_key"),
        max_retries=3,
    )
    tag_model: str = tagger["model"]

    # embedding 配置（chat.sticker_meme.embedding）
    sm: dict = chat_cfg.get("sticker_meme") or {}
    emb_cfg: dict = sm.get("embedding") or {}
    if not emb_cfg.get("model"):
        print("错误：配置文件中未配置 chat.sticker_meme.embedding.model，请先配置")
        return 1
    emb_provider: dict = emb_cfg.get("provider") or {}
    embedding = EmbeddingClient(
        emb_cfg["model"],
        base_url=emb_provider.get("base_url"),
        api_key=emb_provider.get("api_key"),
    )

    if not CHAT_CONFIGS_PATH.is_file():
        print(f"错误：找不到 {CHAT_CONFIGS_PATH}")
        return 1
    chat_configs = json.loads(CHAT_CONFIGS_PATH.read_text("utf-8"))
    memes_dict: dict = chat_configs.get("memes") or {}
    if not memes_dict:
        print("chat_configs.json 中没有 memes 字段，无可迁移内容")
        return 0

    memes_dir = PROJECT_ROOT / "data" / "chat" / sm.get("memes_dir", "memes/")
    index_path = PROJECT_ROOT / "data" / "chat" / sm.get("index_file", "memes_index.json")
    matrix_path = PROJECT_ROOT / "data" / "chat" / sm.get("matrix_file", "memes_index.npy")

    # 收集迁移任务
    tasks: list[tuple[str, str | None, Path]] = []  # (类别, 描述, 图片路径)
    for name, desc in memes_dict.items():
        cat_dir = memes_dir / name
        if not cat_dir.is_dir():
            print(f"警告：类别目录不存在，跳过：{cat_dir}")
            continue
        for p in sorted(cat_dir.iterdir()):
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                tasks.append((name, desc, p))
    if not tasks:
        print("旧类别目录下没有可迁移的图片")
        return 0

    print(f"共 {len(tasks)} 张表情包待迁移（{args.concurrency} 并发打标，模型 {tag_model}）")
    tagging_prompt = _load_tagging_prompt()
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    results: list[tuple[str, str | None, Path, list[str]]] = []  # (类别, 描述, 路径, tags)
    failed: list[Path] = []
    done_count = 0

    async def run_one(category: str, desc: str | None, path: Path) -> None:
        nonlocal done_count
        async with semaphore:
            done_count += 1
            print(f"[{done_count}/{len(tasks)}] {category}/{path.name} …")
            data = path.read_bytes()
            import magic

            mime = magic.from_buffer(data[:64], mime=True)
            tags = await _tag_image(tag_client, tag_model, tagging_prompt, data, mime)
            if not tags:
                failed.append(path)
            else:
                results.append((category, desc, path, tags))
            print(f"    标签：{' '.join(tags) if tags else '（打标失败）'}")

    await asyncio.gather(*(run_one(c, d, p) for c, d, p in tasks))

    if args.dry_run:
        print(
            f"\n[dry-run] 打标完成：成功 {len(results)} 张，失败 {len(failed)} 张，未写入任何数据"
        )
        return 0

    # 写入索引：类别描述并入标签
    index = MemesIndex(
        memes_dir,
        index_path,
        embedding,
        matrix_path,
        topk=int(sm.get("topk", 5)),
        min_similarity=float(sm.get("min_similarity", 0.3)),
        softmax_scale=float(sm.get("softmax_scale", 55.0)),
    )
    index.load()

    migrated = 0
    for category, desc, path, tags in results:
        merged = tags if not desc else [*tags, desc]
        await index.add_image(path, merged)
        migrated += 1
    for category, desc, path in [(c, d, p) for c, d, p in tasks if p in failed]:
        # 打标失败：以待打标状态收录，类别描述仍并入，供事后「改标」修正
        await index.add_image(path, [desc] if desc else [])
        migrated += 1

    print(
        f"\n迁移完成：{migrated} 张已收录（打标失败待补标 {len(failed)} 张），索引写入 {index_path}"
    )

    if args.apply:
        chat_configs.pop("memes", None)
        CHAT_CONFIGS_PATH.write_text(
            json.dumps(chat_configs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已从 {CHAT_CONFIGS_PATH} 移除 memes 字段")
        for name in memes_dict:
            cat_dir = memes_dir / name
            try:
                if cat_dir.is_dir() and not any(cat_dir.iterdir()):
                    cat_dir.rmdir()
                    print(f"已清理空类别目录：{cat_dir}")
            except OSError as e:
                print(f"警告：清理类别目录失败 {cat_dir}: {e}")
        print("旧协议残留已清理，新表情包系统即刻可用")
        print(
            "提示：旧类别目录（含原图）已保留作备份，确认新系统正常后可手动删除 data/chat/memes/ 下的旧子目录"
        )
    else:
        print("提示：确认无误后加 --apply 重新运行，以清理旧 memes 字段与空类别目录")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
