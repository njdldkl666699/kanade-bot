import rpyc
from nonebot import get_driver, logger

from .config import cfg

client = None


def query(query_text: str) -> list[str] | None:
    """执行RAG查询，返回相似度分数低于阈值的相关文档列表"""
    if client is None:
        logger.warning("RAG功能未启用，无法执行查询")
        return None
    try:
        results = client.root.query(
            query_text,
            n_results=cfg.chat_rag_query_n_results,
            threshold=cfg.chat_rag_distance_threshold,
        )
        return results
    except Exception as e:
        logger.error(f"执行RAG查询时发生错误: {e}")
        return None


driver = get_driver()


@driver.on_startup
async def startup():
    if not cfg.chat_rag_enabled:
        logger.info("RAG功能已禁用，跳过RAG RPC客户端初始化")
        return
    logger.info("RAG功能已启用，正在连接RAG RPC服务器...")

    global client
    port = cfg.chat_rag_port

    try:
        client = rpyc.connect("localhost", port)
        logger.info(f"RAG RPC客户端已连接到服务器 localhost:{port}")
    except Exception as e:
        logger.error(f"连接RAG RPC服务器失败: {e}")
        client = None
