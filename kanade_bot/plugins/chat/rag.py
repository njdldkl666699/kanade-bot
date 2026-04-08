import time

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
from nonebot import get_driver, logger
from pydantic import BaseModel

from .config import cfg

collection: chromadb.Collection | None = None


class QueryResult(BaseModel):
    """RAG查询结果"""

    documents: list[str]
    """检索到的相关文档列表"""
    distances: list[float]
    """每个相关文档与查询的相似度分数列表，数值越小表示越相关"""


def query(query_text: str) -> QueryResult | None:
    """执行RAG查询，返回相关文档和相似度分数"""
    if collection is None:
        logger.error("向量数据库集合未初始化，无法执行查询")
        return

    results = collection.query(
        query_texts=query_text,
        n_results=cfg.chat_rag_query_n_results,
    )

    documents = results["documents"]
    if documents is None:
        return
    distances = results["distances"]
    if distances is None:
        return

    return QueryResult(documents=documents[0], distances=distances[0])


def query_with_score(query_text: str) -> list[str]:
    """执行RAG查询，返回相似度分数低于阈值的相关文档列表"""
    result = query(query_text)
    if result is None:
        return []

    relevant_documents: list[str] = []
    for doc, score in zip(result.documents, result.distances):
        if score <= cfg.chat_rag_score_threshold:
            relevant_documents.append(doc)

    return relevant_documents


driver = get_driver()


@driver.on_startup
async def startup():
    global collection
    if not cfg.chat_rag_enabled:
        logger.info("RAG模块已禁用，跳过向量数据库初始化")
        return

    logger.info("RAG模块正在启动，正在初始化向量数据库客户端和集合...")

    start = time.time()

    bge_ef = SentenceTransformerEmbeddingFunction(
        model_name=cfg.chat_rag_model_or_path,
        normalize_embeddings=True,
    )

    client = chromadb.PersistentClient(path=cfg.chat_rag_db_persistent_path)

    collection = client.get_collection(
        cfg.chat_rag_db_collection_name,
        embedding_function=bge_ef,  # pyright: ignore[reportArgumentType]
    )

    end = time.time()

    logger.info(f"RAG模块向量数据库客户端和集合初始化完成，耗时{end - start:.2f}秒")
