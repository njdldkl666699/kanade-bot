"""OpenAI 兼容 embeddings 端点封装与向量工具"""

import numpy as np
from openai import AsyncOpenAI

EMBED_BATCH_SIZE = 50
"""embedding API 单次请求的最大条数"""


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


def normalize(vec: list[float]):
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v
