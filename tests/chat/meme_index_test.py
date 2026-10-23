"""meme_index 单元测试：索引 CRUD、检索加权抽取与持久化往返

embedding 用确定性 fake：每个词按首字符序号映射到固定维度累加，
保证首字符相同的词向量相似、不同的词向量正交。
异步方法统一用 `asyncio.run` 驱动（项目未引入 pytest-asyncio）。

模块加载：meme_index 位于 chat 插件目录内，但 chat 包的 `__init__`
依赖已初始化的 nonebot，故按文件路径 spec 加载（同 test_prompt_sections 模式）。
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "chat_meme_index",
    Path(__file__).parents[2] / "kanade_bot" / "plugins" / "chat" / "meme_index.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_meme_index = importlib.util.module_from_spec(_SPEC)
sys.modules["chat_meme_index"] = _meme_index
_SPEC.loader.exec_module(_meme_index)

from chat_meme_index import (
    EmbeddingClient,
    MemeRecord,
    MemesIndex,
    dedupe_tags,
    parse_model_tags,
    parse_tags,
)

DIMS = 16


class FakeEmbedding(EmbeddingClient):
    """确定性假向量：词按首字符序号映射维度，文本向量为词袋叠加"""

    def __init__(self, model: str = "fake-model"):
        self.model = model
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vec(t) for t in texts]

    @staticmethod
    def _vec(text: str) -> list[float]:
        v = [0.0] * DIMS
        for w in text.split():
            v[ord(w[0]) % DIMS] += 1.0
        return v


def make_image(directory, name: str = "img.png"):
    p = directory / name
    p.write_bytes(b"fake")
    return p


@pytest.fixture
def index(tmp_path):
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    return MemesIndex(
        memes_dir,
        tmp_path / "memes_index.json",
        FakeEmbedding(),
        topk=3,
        min_similarity=0.3,
        softmax_scale=55.0,
    )


def test_parse_tags_separators():
    assert parse_tags("开心 大笑，无语；测试、嗯嗯 | ok") == [
        "开心",
        "大笑",
        "无语",
        "测试",
        "嗯嗯",
        "ok",
    ]
    assert parse_tags("") == []


def test_parse_model_tags_json_and_fallback():
    assert parse_model_tags('好的，这是标签：\n["猫猫", "开心"]') == ["猫猫", "开心"]
    assert parse_model_tags('```json\n["猫猫"]\n```') == ["猫猫"]
    assert parse_model_tags("猫猫 开心") == ["猫猫", "开心"]
    assert dedupe_tags(["a", "a", "b"]) == ["a", "b"]


def test_record_tag_text():
    assert MemeRecord(id=1, file="a.png", tags=["x", "y"]).tag_text == "x y"


def test_select_basic(index):
    async def _run():
        await index.add_image(make_image(index.memes_dir, "a.png"), ["开心", "大笑"])
        await index.add_image(make_image(index.memes_dir, "b.png"), ["伤心", "难过"])
        return await index.select("开心"), await index.select("伤心")

    hit_a, hit_b = asyncio.run(_run())
    assert hit_a is not None and hit_a.tags == ["开心", "大笑"]
    assert hit_b is not None and hit_b.tags == ["伤心", "难过"]


def test_select_threshold_filters(index):
    async def _run():
        await index.add_image(make_image(index.memes_dir, "a.png"), ["伤心", "难过"])
        return await index.select("开心", min_similarity=0.3)

    # "开心"与"伤心"首字符不同 → 向量正交，相似度 0，被阈值过滤
    assert asyncio.run(_run()) is None


def test_select_returns_none_on_empty(index):
    assert asyncio.run(index.select("任意")) is None


def test_needs_tag_excluded(index):
    async def _run():
        await index.add_image(make_image(index.memes_dir, "a.png"), ["开心"])
        rec = await index.add_image(make_image(index.memes_dir, "b.png"), [])
        hits = [await index.select("开心") for _ in range(10)]
        return rec, hits

    rec, hits = asyncio.run(_run())
    assert rec.needs_tag
    for hit in hits:
        assert hit is not None and hit.file != rec.file


def test_retag_and_remove(index):
    async def _run():
        rec = await index.add_image(make_image(index.memes_dir, "a.png"), ["开心"])
        updated = await index.retag(rec.id, ["伤心", "大哭"])
        hit = await index.select("伤心")
        removed = index.remove(rec.id)
        after = await index.select("伤心")
        return rec, updated, hit, removed, after

    rec, updated, hit, removed, after = asyncio.run(_run())
    assert not index.file_path(rec).exists()
    assert updated is not None
    assert updated.tags == ["伤心", "大哭"]
    assert not updated.needs_tag
    assert hit is not None
    assert removed
    assert index.get(rec.id) is None
    assert after is None


def test_persist_roundtrip(index):
    async def _add():
        await index.add_image(make_image(index.memes_dir, "a.png"), ["开心", "大笑"])
        await index.add_image(make_image(index.memes_dir, "b.png"), ["伤心"])

    asyncio.run(_add())
    assert index.index_path.is_file()

    async def _verify(reloaded: MemesIndex):
        reloaded.load()
        rec = await reloaded.select("开心")
        return len(reloaded.records), reloaded.needs_rebuild, rec

    reloaded = MemesIndex(index.memes_dir, index.index_path, FakeEmbedding())
    count, needs_rebuild, rec = asyncio.run(_verify(reloaded))
    assert not needs_rebuild
    assert count == 2
    assert rec is not None and rec.tags == ["开心", "大笑"]


def test_model_mismatch_triggers_rebuild_flag(tmp_path):
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()

    async def _add():
        idx = MemesIndex(memes_dir, tmp_path / "i.json", FakeEmbedding("model-a"))
        await idx.add_image(make_image(memes_dir), ["开心"])

    asyncio.run(_add())

    async def _rebuild():
        # 换模型加载：向量被清空并要求重建，随后重建恢复检索
        idx2 = MemesIndex(memes_dir, tmp_path / "i.json", FakeEmbedding("model-b"))
        idx2.load()
        flagged = idx2.needs_rebuild
        cleared = all(not r.embedding for r in idx2.records)
        rebuilt = await idx2.rebuild_embeddings()
        hit = await idx2.select("开心")
        return flagged, cleared, rebuilt, hit, idx2.needs_rebuild

    flagged, cleared, rebuilt, hit, still_flagged = asyncio.run(_rebuild())
    assert flagged
    assert cleared
    assert rebuilt == 1
    assert not still_flagged
    assert hit is not None


def test_index_json_structure(index):
    async def _run():
        await index.add_image(make_image(index.memes_dir), ["开心"])

    asyncio.run(_run())
    data = json.loads(index.index_path.read_text("utf-8"))
    assert data["meta"]["embedding_model"] == "fake-model"
    assert len(data["records"]) == 1
    assert data["records"][0]["tags"] == ["开心"]
    assert "embedding" not in data["records"][0], "json 不应再内嵌向量"

    matrix = np.load(index.matrix_path, allow_pickle=False)
    assert matrix.shape == (1, DIMS)
    assert matrix.dtype == np.float32


def test_legacy_json_auto_migrates(tmp_path):
    """旧格式（json 内嵌 embedding）加载后自动转存为 json + npy 双文件"""
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    index_path = tmp_path / "i.json"
    vec = [1.0] + [0.0] * (DIMS - 1)
    index_path.write_text(
        json.dumps(
            {
                "meta": {"embedding_model": "fake-model"},
                "records": [
                    {
                        "id": 1,
                        "file": "a.png",
                        "tags": ["开"],
                        "needs_tag": False,
                        "added_at": 1.0,
                        "embedding": vec,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    idx = MemesIndex(memes_dir, index_path, FakeEmbedding())
    idx.load()
    assert not idx.needs_rebuild
    assert idx.matrix_path.is_file(), "旧格式应已转存出 .npy"

    data = json.loads(index_path.read_text("utf-8"))
    assert "embedding" not in data["records"][0]
    assert asyncio.run(idx.select("开")) is not None


def test_matrix_row_mismatch_triggers_rebuild(tmp_path):
    """矩阵行数与记录数不符（写入中断残留）→ 置 needs_rebuild 并可重建恢复"""
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    index_path = tmp_path / "i.json"

    async def _add():
        idx = MemesIndex(memes_dir, index_path, FakeEmbedding())
        await idx.add_image(make_image(memes_dir, "a.png"), ["开心"])
        await idx.add_image(make_image(memes_dir, "b.png"), ["伤心"])
        return idx

    asyncio.run(_add())

    # 模拟崩溃残留：矩阵比 json 少一行
    matrix = np.load(index_path.with_suffix(".npy"), allow_pickle=False)
    np.save(index_path.with_suffix(".npy"), matrix[:-1])

    async def _recover():
        idx2 = MemesIndex(memes_dir, index_path, FakeEmbedding())
        idx2.load()
        flagged = idx2.needs_rebuild
        rebuilt = await idx2.rebuild_embeddings()
        hit = await idx2.select("开心")
        return flagged, rebuilt, hit

    flagged, rebuilt, hit = asyncio.run(_recover())
    assert flagged
    assert rebuilt == 2
    assert hit is not None


def test_custom_matrix_path(tmp_path):
    """matrix_path 可独立于 index_path 指定，缺省时从索引文件名派生"""
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    index_path = tmp_path / "i.json"
    matrix_path = tmp_path / "vectors" / "custom.npy"

    async def _run():
        idx = MemesIndex(memes_dir, index_path, FakeEmbedding(), matrix_path)
        assert idx.matrix_path == matrix_path
        await idx.add_image(make_image(memes_dir, "a.png"), ["开心"])
        reloaded = MemesIndex(memes_dir, index_path, FakeEmbedding(), matrix_path)
        reloaded.load()
        return reloaded, await reloaded.select("开心")

    reloaded, hit = asyncio.run(_run())
    assert matrix_path.is_file()
    assert not index_path.with_suffix(".npy").exists(), "缺省派生路径不应被写入"
    assert not reloaded.needs_rebuild
    assert hit is not None

    derived = MemesIndex(memes_dir, index_path, FakeEmbedding())
    assert derived.matrix_path == index_path.with_suffix(".npy")


def test_adopt_file_and_unindexed_dangling(tmp_path):
    """adopt_file 收录目录内已有文件（不复制）；unindexed/dangling 目录扫描"""
    memes_dir = tmp_path / "memes"
    memes_dir.mkdir()
    index = MemesIndex(memes_dir, tmp_path / "i.json", FakeEmbedding())

    async def _run():
        manual = make_image(memes_dir, "manual.png")
        adopted = await index.adopt_file(manual, ["开心"])
        return manual, adopted

    manual, adopted = asyncio.run(_run())
    assert adopted.file == "manual.png", "保留原文件名"
    assert list(memes_dir.iterdir()) == [manual], "不应产生副本"
    assert asyncio.run(index.select("开心")) is not None
    assert index.unindexed_files() == [], "已收录文件不再被视为未索引"

    # 未索引文件被发现；非图片扩展名被忽略
    extra = make_image(memes_dir, "extra.gif")
    ignored = memes_dir / "note.txt"
    ignored.write_text("x")
    assert index.unindexed_files() == [extra]

    # 同名重复收录抛错
    try:
        asyncio.run(index.adopt_file(manual, ["重复"]))
        raise AssertionError("应抛 ValueError")
    except ValueError:
        pass

    # 文件被手动删除 → 悬空记录
    manual.unlink()
    dangling = index.dangling_records()
    assert [r.id for r in dangling] == [adopted.id]
