"""表情包语义检索子系统：索引装配与生命周期管理

图片由视觉模型自动打标（tagging），LLM 以 `{{表情: 描述}}` 占位符引用，
投递时按语义相似度加权随机选图（index）。
"""

from nonebot import get_driver, logger

from ..config import cfg
from .embedding import EmbeddingClient
from .index import MemesIndex

_memes: MemesIndex | None = None


def get_memes() -> MemesIndex | None:
    """获取表情包索引实例；未启用时返回 None"""
    return _memes


def memes_available() -> bool:
    """表情包语义检索是否可用"""
    return _memes is not None


driver = get_driver()


@driver.on_startup
async def _startup() -> None:
    global _memes
    m = cfg.sticker_meme
    if not m.enabled or not m.embedding.model:
        if m.enabled:
            logger.warning(
                "表情包语义检索已启用但未配置 embedding 模型（chat.sticker_meme.embedding.model），跳过初始化"
            )
        return

    provider = m.embedding.provider
    embedding = EmbeddingClient(
        m.embedding.model,
        base_url=provider.base_url if provider else None,
        api_key=provider.api_key if provider else None,
    )
    memes = MemesIndex(
        m.memes_dir_path,
        m.index_file_path,
        embedding,
        m.matrix_file_path,
        topk=m.topk,
        min_similarity=m.min_similarity,
        softmax_scale=m.softmax_scale,
    )
    memes.load()
    if memes.needs_rebuild:
        logger.warning("表情包索引向量与当前 embedding 模型不符，开始全量重建…")
        try:
            rebuilt = await memes.rebuild_embeddings()
            logger.info(f"表情包索引向量重建完成：{rebuilt} 条")
        except Exception:
            logger.exception("表情包索引向量重建失败，待打标记录将在下次启动时重试")
            memes.needs_rebuild = False

    _memes = memes
    pending = sum(1 for r in memes.records if r.needs_tag)
    logger.info(f"表情包索引已加载：{len(memes.records)} 条记录（待打标 {pending} 条）")
