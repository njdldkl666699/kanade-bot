"""表情包语义索引：目录即真相，索引可从目录重建。"""

import asyncio
import json
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from nonebot import logger
from openai import AsyncOpenAI

TAGGING_FALLBACK_PROMPT = """你是表情包标注助手。请查看这张表情包图片，为它生成用于语义检索的中文标签。

标注要求：
1. 输出一个 JSON 数组，包含 5~10 个简短的中文标签
2. 覆盖以下维度（按图适用性取舍）：
   - 画面主体：角色/人物/生物，如"猫猫"、"小女孩"、"熊猫头"
   - 情绪状态：如"开心"、"无语"、"暴怒"、"委屈"
   - 动作神态：如"手舞足蹈"、"探头偷看"、"瘫倒"
   - 图内文字的语义：表达图内文字的含义或梗，而非抄录原文
   - 适用场景：什么话题下会想发这张图，如"敷衍回复"、"阴阳怪气"、"庆祝"
3. 标签是检索关键词：想象群友想发这张图时会怎么描述它，用大家会用的口语化说法
4. 每个标签 2~6 个字
5. 只输出 JSON 数组本身，不要输出任何其他内容

输出示例：
["猫猫", "开心", "手舞足蹈", "庆祝", "得意", "偷着乐"] 
"""

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
"""允许收录的图片扩展名"""

EMBED_BATCH_SIZE = 50
"""embedding API 单次请求的最大条数"""

_TAG_SPLIT = re.compile(r"[\s,，;；|、]+")
_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]")


def parse_tags(text: str) -> list[str]:
    """把一段文本切分为标签列表：空白/中英文逗号/分号/竖线/顿号均可作分隔符"""
    out: list[str] = []
    seen: set[str] = set()
    for part in _TAG_SPLIT.split(str(text or "").strip()):
        if part and part not in seen:
            seen.add(part)
            out.append(part)
    return out


def dedupe_tags(tags: list[str]) -> list[str]:
    """去重保序"""
    return parse_tags(" ".join(tags))


@dataclass
class MemeRecord:
    """一条表情包索引记录"""

    id: int
    """记录 ID（自增，供命令引用）"""

    file: str
    """文件名，相对 memes_dir（扁平目录）"""

    tags: list[str]
    """语义标签，拼接文本即向量化的语料"""

    needs_tag: bool = False
    """无有效标签，待补标；检索时排除"""

    added_at: float = field(default_factory=time.time)
    """收录时间戳"""

    embedding: list[float] = field(default_factory=list)
    """tags 文本的向量；空列表表示无向量"""

    @property
    def tag_text(self) -> str:
        return " ".join(self.tags)


class EmbeddingClient:
    """OpenAI 兼容 embeddings 端点的最小封装"""

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None):
        import os

        self.model = model
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            max_retries=3,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = await self._client.embeddings.create(input=texts, model=self.model)
        return [item.embedding for item in resp.data]

    async def embed_batched(self, texts: list[str]) -> list[list[float]]:
        """分批 embed，规避单次请求条数限制"""
        out: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            out.extend(await self.embed(texts[i : i + EMBED_BATCH_SIZE]))
        return out


def _normalize(vec: list[float]) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def parse_model_tags(raw: str) -> list[str]:
    """宽容解析打标模型的输出：提取首个 JSON 字符串数组；失败时按分隔符切分"""
    text = str(raw or "").strip()
    if match := _JSON_ARRAY.search(text):
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, list):
                return dedupe_tags([str(x) for x in obj if str(x).strip()])
        except json.JSONDecodeError:
            pass
    return parse_tags(text)


class MemesIndex:
    """表情包向量索引：双文件持久化 + 内存矩阵检索"""

    def __init__(
        self,
        memes_dir: Path,
        index_path: Path,
        embedding: EmbeddingClient,
        matrix_path: Path | None = None,
        *,
        topk: int = 5,
        min_similarity: float = 0.3,
        softmax_scale: float = 55.0,
    ):
        self.memes_dir = memes_dir
        self.index_path = index_path
        self.matrix_path = matrix_path or index_path.with_suffix(".npy")
        self.embedding = embedding
        self.topk = topk
        self.min_similarity = min_similarity
        self.softmax_scale = softmax_scale

        self._records: list[MemeRecord] = []
        self._matrix: np.ndarray | None = None
        self._meta: dict = {}
        self.needs_rebuild = False
        """load 后为真表示库内向量与当前 embedding 模型不符，需调用 rebuild_embeddings"""
        self._lock = asyncio.Lock()

    # ---------- 查询 ----------

    @property
    def records(self) -> list[MemeRecord]:
        return list(self._records)

    def get(self, rec_id: int) -> MemeRecord | None:
        return next((r for r in self._records if r.id == rec_id), None)

    def file_path(self, rec: MemeRecord) -> Path:
        return self.memes_dir / rec.file

    async def select(
        self,
        query: str,
        *,
        k: int | None = None,
        min_similarity: float | None = None,
    ) -> MemeRecord | None:
        """按查询文本检索一张表情包，无候选或失败返回 None"""
        if not query.strip() or self._matrix is None or not self._records:
            return None
        try:
            q_raw = (await self.embedding.embed([query]))[0]
        except Exception:
            logger.exception("表情包查询向量生成失败：%s", query)
            return None
        q = _normalize(q_raw)
        if q.shape[0] != self._matrix.shape[1]:
            logger.error(
                "查询向量维度 %s 与索引维度 %s 不符（embedding 模型不一致？）",
                q.shape[0],
                self._matrix.shape[1],
            )
            return None

        k = self.topk if k is None else k
        threshold = self.min_similarity if min_similarity is None else min_similarity
        scores = self._matrix @ q
        cands = [
            (rec, float(s))
            for rec, s in zip(self._records, scores)
            if not rec.needs_tag and rec.embedding and s >= threshold
        ]
        if not cands:
            return None
        cands.sort(key=lambda x: x[1], reverse=True)
        cands = cands[: max(1, k)]

        raw = np.asarray([s for _, s in cands], dtype=np.float32) * self.softmax_scale
        z = np.exp(raw - np.max(raw))
        weights = (z / z.sum()).tolist()
        return random.choices(cands, weights=weights, k=1)[0][0]

    # ---------- 写操作 ----------

    async def add_image(self, image_path: Path, tags: list[str]) -> MemeRecord:
        """收录一张图片：复制进 memes_dir，打向量并写索引

        tags 为空时以 needs_tag 状态入库（不参与检索）。
        """
        tags = dedupe_tags(tags)
        self.memes_dir.mkdir(parents=True, exist_ok=True)
        dest = self.memes_dir / f"{uuid.uuid4().hex}{image_path.suffix.lower()}"
        if image_path.resolve() != dest.resolve():
            shutil.copy(image_path, dest)

        rec = MemeRecord(id=0, file=dest.name, tags=tags, needs_tag=not tags)
        if tags:
            rec.embedding = (await self.embedding.embed([rec.tag_text]))[0]

        async with self._lock:
            rec.id = self._next_id()
            self._records.append(rec)
            self._append_matrix_row(rec)
            self._write_meta()
            self.save()
        return rec

    async def retag(self, rec_id: int, tags: list[str]) -> MemeRecord | None:
        """更新记录标签并重算向量"""
        rec = self.get(rec_id)
        if rec is None:
            return None
        tags = dedupe_tags(tags)
        embedding = (
            (await self.embedding.embed([MemeRecord(0, "", tags).tag_text]))[0] if tags else []
        )
        async with self._lock:
            rec.tags = tags
            rec.needs_tag = not tags
            rec.embedding = embedding
            self._set_matrix_row(rec)
            self._write_meta()
            self.save()
        return rec

    def remove(self, rec_id: int) -> bool:
        """删除记录及其图片文件"""
        rec = self.get(rec_id)
        if rec is None:
            return False
        self.file_path(rec).unlink(missing_ok=True)
        idx = self._records.index(rec)
        self._records.pop(idx)
        if self._matrix is not None and self._matrix.shape[0]:
            self._matrix = np.delete(self._matrix, idx, axis=0)
        self.save()
        return True

    async def rebuild_embeddings(self) -> int:
        """按当前 embedding 模型重算所有有标签记录的向量，返回重算条数"""
        targets = [r for r in self._records if r.tags]
        texts = [r.tag_text for r in targets]
        vecs = await self.embedding.embed_batched(texts)
        async with self._lock:
            for rec, vec in zip(targets, vecs):
                rec.embedding = vec
            self._write_meta()
            self._rebuild_matrix()
            self.save()
        self.needs_rebuild = False
        return len(targets)

    # ---------- 持久化 ----------

    def load(self) -> None:
        """从索引文件加载记录并构建矩阵；异常情况置 needs_rebuild"""
        legacy = False
        if self.index_path.is_file():
            try:
                data = json.loads(self.index_path.read_text("utf-8"))
                self._meta = data.get("meta", {})
                raw_records = data.get("records", [])
                legacy = any("embedding" in r for r in raw_records)
                self._records = [MemeRecord(**r) for r in raw_records]
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.exception("表情包索引文件损坏：%s", self.index_path)
                self._records = []
                self._meta = {}
        else:
            self._records = []
            self._meta = {}

        if not self._load_matrix():
            self._rebuild_matrix()
        if legacy:
            self.save()

        model = str(self._meta.get("embedding_model") or "")
        if model and model != self.embedding.model:
            stale = sum(1 for r in self._records if r.embedding)
            for r in self._records:
                r.embedding = []
            self.needs_rebuild = True
            if stale:
                logger.warning(
                    "索引向量由 %s 生成（当前模型 %s），%s 条向量待重建",
                    model,
                    self.embedding.model,
                    stale,
                )

    def _load_matrix(self) -> bool:
        """读取向量矩阵文件并回填记录向量；成功返回 True"""
        if not self.matrix_path.is_file():
            return False
        try:
            matrix = np.load(self.matrix_path, allow_pickle=False)
        except Exception:
            logger.exception("表情包向量矩阵文件损坏：%s", self.matrix_path)
            return False
        if matrix.ndim == 1:
            matrix = np.zeros((0, 0), np.float32)  # 空库占位
        if matrix.ndim != 2 or matrix.shape[0] != len(self._records):
            logger.warning(
                "向量矩阵行数 %s 与记录数 %s 不符（写入中断残留），向量待重建",
                matrix.shape[0] if matrix.ndim == 2 else "-",
                len(self._records),
            )
            self.needs_rebuild = True
            return False

        self._matrix = matrix.astype(np.float32, copy=False)
        if self._matrix is None:
            return False
        for i, rec in enumerate(self._records):
            row = self._matrix[i]
            rec.embedding = row.tolist() if float(np.linalg.norm(row)) > 0 else []
        return True

    def save(self) -> None:
        """原子写索引文件"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.matrix_path.parent.mkdir(parents=True, exist_ok=True)
        matrix = self._matrix if self._matrix is not None else np.zeros((0, 0), np.float32)
        npy_tmp = self.matrix_path.with_name(self.matrix_path.name + ".tmp")
        with npy_tmp.open("wb") as f:
            np.save(f, matrix)
        npy_tmp.replace(self.matrix_path)

        payload = {
            "meta": self._meta,
            "records": [
                {
                    "id": r.id,
                    "file": r.file,
                    "tags": r.tags,
                    "needs_tag": r.needs_tag,
                    "added_at": r.added_at,
                }
                for r in self._records
            ],
        }
        tmp = self.index_path.with_name(self.index_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    # ---------- 内部 ----------

    def _next_id(self) -> int:
        return max((r.id for r in self._records), default=0) + 1

    def _write_meta(self) -> None:
        self._meta["embedding_model"] = self.embedding.model
        self._meta["updated_at"] = time.time()

    def _dim(self) -> int:
        return self._matrix.shape[1] if self._matrix is not None else 0

    def _row_of(self, rec: MemeRecord) -> np.ndarray:
        vec = (
            _normalize(rec.embedding)
            if rec.embedding
            else np.zeros(max(self._dim(), 1), np.float32)
        )
        if self._dim() and vec.shape[0] != self._dim():
            raise ValueError(
                f"记录 #{rec.id} 向量维度 {vec.shape[0]} 与索引维度 {self._dim()} 不符"
            )
        return vec

    def _append_matrix_row(self, rec: MemeRecord) -> None:
        row = self._row_of(rec)
        self._matrix = np.stack([row]) if self._matrix is None else np.vstack([self._matrix, row])

    def _set_matrix_row(self, rec: MemeRecord) -> None:
        idx = next(i for i, r in enumerate(self._records) if r.id == rec.id)
        if self._matrix is None:
            self._append_matrix_row(rec)
            return
        if not self._dim():
            self._matrix = np.stack([self._row_of(rec)])
            return
        self._matrix[idx] = self._row_of(rec)

    def _rebuild_matrix(self) -> None:
        dims = {len(r.embedding) for r in self._records if r.embedding}
        if len(dims) > 1:
            logger.warning("索引中存在多种向量维度 %s，仅保留众数维度", dims)
        dim = dims.pop() if len(dims) == 1 else 0
        rows = [
            _normalize(r.embedding)
            if (r.embedding and len(r.embedding) == dim)
            else np.zeros(dim, np.float32)
            for r in self._records
        ]
        for r in self._records:
            if r.embedding and len(r.embedding) != dim:
                r.embedding = []
        self._matrix = np.stack(rows) if rows else None
