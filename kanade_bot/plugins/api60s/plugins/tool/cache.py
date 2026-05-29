from pydantic import BaseModel

from kanade_bot.plugins.api60s.client import client


class TranslateLang(BaseModel):
    """翻译语言项"""

    code: str
    label: str
    alphabet: str


class TranslateLangCache:
    """翻译语言缓存"""

    _langs: list[TranslateLang] = []
    _code_index: dict[str, TranslateLang] = {}

    @classmethod
    async def _load_cache(cls):
        response = await client.get("/v2/fanyi/langs")
        data = response.json().get("data", [])
        cls._langs = [TranslateLang.model_validate(item) for item in data]
        cls._code_index = {lang.code.lower(): lang for lang in cls._langs}

    @classmethod
    async def get_langs(cls) -> list[TranslateLang]:
        if not cls._langs:
            await cls._load_cache()
        return cls._langs

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
        exact = cls._code_index.get(normalized.lower())
        if exact:
            return [exact]

        # 其次尝试标签的模糊匹配
        return [lang for lang in langs if normalized in lang.label]
