from pydantic import BaseModel

from copilot_bot.plugins.api60s.client import client


class TranslateLang(BaseModel):
    """翻译语言项"""

    code: str
    label: str
    alphabet: str


class TranslateLangCache:
    """翻译语言缓存"""

    __langs: list[TranslateLang] = []
    __code_index: dict[str, TranslateLang] = {}

    @classmethod
    async def _load_cache(cls):
        response = await client.get("/v2/fanyi/langs")
        data = response.json().get("data", [])
        cls.__langs = [TranslateLang.model_validate(item) for item in data]
        cls.__code_index = {lang.code.lower(): lang for lang in cls.__langs}

    @classmethod
    async def get_langs(cls) -> list[TranslateLang]:
        if not cls.__langs:
            await cls._load_cache()
        return cls.__langs

    @classmethod
    async def query_langs(cls, query: str | None) -> list[TranslateLang]:
        """查询语言，支持模糊匹配标签和代码"""
        langs = await cls.get_langs()
        if query is None:
            return []

        normalized = query.strip()
        if not normalized:
            return []

        # 优先尝试代码的精确匹配
        exact = cls.__code_index.get(normalized.lower())
        if exact:
            return [exact]

        # 其次尝试标签的模糊匹配
        return [lang for lang in langs if normalized in lang.label]
