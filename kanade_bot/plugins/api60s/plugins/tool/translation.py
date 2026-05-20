from kanade_bot.plugins.api60s.client import client

from .cache import TranslateLang, TranslateLangCache


def _format_langs(langs: list[TranslateLang]) -> str:
    if not langs:
        return "未查询到匹配语言。"

    lines = ["查询到以下语言："]
    for lang in langs:
        lines.append(f"- {lang.label} ({lang.code})")
    return "\n".join(lines)


async def process_translation(
    text: str,
    from_query: str | None = None,
    to_query: str | None = None,
) -> str:
    if not text:
        return "请输入要翻译的文本。"

    to_matches = await TranslateLangCache.query_langs(to_query)
    if to_query is None or to_query.lower() == "auto":
        to_lang = "auto"
    elif len(to_matches) == 1:
        to_lang = to_matches[0].code
    else:
        return _format_langs(to_matches)

    from_matches = await TranslateLangCache.query_langs(from_query)
    if from_query is None or from_query.lower() == "auto":
        from_lang = "auto"
    elif len(from_matches) == 1:
        from_lang = from_matches[0].code
    else:
        return _format_langs(from_matches)

    response = await client.get(
        "/v2/fanyi",
        params={
            "text": text,
            "from": from_lang,
            "to": to_lang,
            "encoding": "text",
        },
    )
    return response.text
