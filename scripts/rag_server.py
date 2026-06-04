import os
import time
from typing import Literal

from chromadb import Collection, PersistentClient
from chromadb.utils.embedding_functions import (
    OpenAIEmbeddingFunction,
    SentenceTransformerEmbeddingFunction,
)
from loguru import logger
from pydantic import BaseModel
from rpyc import Service, ThreadedServer

from .kanade_wiki import DOCUMENTS, IDS, METADATAS
from .util import get_config


class ScopedConfig(BaseModel):
    """RAG服务器的配置类，包含了RAG服务器需要的配置项"""

    port: int = 39831
    """RAG服务器端口号，需要与RAG客户端的配置一致"""
    db_dir_path: str = "rag/kanade_rag_db/"
    """向量数据库的存储目录路径"""
    collection_name: str = "kanade_wiki_collection"
    """向量数据库中集合的名称"""

    embedding_type: Literal["sentence_transformer", "openai"] = "sentence_transformer"
    """RAG使用的向量化方法
    - `sentence_transformer`需要指定model_name_or_path参数
    - `openai`需要指定openai开头的参数
    """
    model_name_or_path: str = "BAAI/bge-small-zh-v1.5"
    """RAG使用的模型名称或路径，支持从Hugging Face下载"""
    openai_api_key: str | None = None
    """OpenAI API密钥"""
    openai_base_url: str | None = None
    """OpenAI API Base URL，可选，用于自定义端点"""
    openai_model: str = "text-embedding-3-small"
    """OpenAI嵌入模型名称"""


class Config(BaseModel):
    rag: ScopedConfig


cfg = get_config(Config).rag


def get_embedding_function():
    """根据配置获取合适的嵌入函数"""
    if cfg.embedding_type == "openai":
        if not cfg.openai_api_key and not os.getenv("CHROMA_OPENAI_API_KEY"):
            # 没有提供API密钥，也没有设置CHROMA_OPENAI_API_KEY
            raise ValueError("使用OpenAI嵌入时，必须提供openai_api_key")

        logger.info(
            f"使用OpenAI嵌入函数: model={cfg.openai_model}, base_url={cfg.openai_base_url or '默认'}"
        )
        return OpenAIEmbeddingFunction(
            api_key=cfg.openai_api_key,
            model_name=cfg.openai_model,
            api_base=cfg.openai_base_url,
        )

    # 默认使用 Sentence Transformer
    logger.info(f"使用Sentence Transformer嵌入函数: model={cfg.model_name_or_path}")
    return SentenceTransformerEmbeddingFunction(
        model_name=cfg.model_name_or_path,
        normalize_embeddings=True,  # 可选：是否归一化向量，通常推荐在使用余弦相似度时开启
    )


def ensure_data_and_db(collection: Collection):
    """预检文档、ID、元数据数量，并确保数据库中的文档完整"""
    # 预检文档、ID、元数据数量
    n_ids = len(IDS)
    n_metadatas = len(METADATAS)
    n_docs = len(DOCUMENTS)
    logger.info(f"预检 ID数量: {n_ids}, 元数据数量: {n_metadatas}, 文档数量: {n_docs}")
    if not (n_ids == n_metadatas == n_docs):
        raise ValueError("预检失败：ID、元数据和文档的数量不一致！")

    # 简单判定数据库中的文档是否完整，如果不完整则添加文档
    if collection.count() < n_ids:
        logger.info("数据库中文档不完整，正在添加文档...")
        # 先删除可能存在的旧文档
        collection.delete(ids=IDS)
        # 添加新文档
        collection.add(ids=IDS, metadatas=METADATAS, documents=DOCUMENTS)
        logger.info("文档添加完成。")


class RAGService(Service):
    def __init__(self, collection: Collection):
        self.collection = collection

    def exposed_query(
        self,
        query_text: str,
        *,
        n_results: int = 10,
        threshold: float = 0.6,
    ) -> list[str] | None:
        """执行RAG查询，返回相似度分数低于阈值的相关文档列表"""
        logger.info(
            f"收到查询请求: query_text='{query_text}', n_results={n_results}, threshold={threshold}"
        )
        results = self.collection.query(
            query_texts=query_text,
            n_results=n_results,
        )

        documents = results["documents"]
        if documents is None:
            return None
        distances = results["distances"]
        if distances is None:
            return None

        filtered_docs = []
        for doc, dist in zip(documents[0], distances[0]):
            if dist < threshold:
                filtered_docs.append(doc)

        return filtered_docs


def main():
    start = time.time()
    embedding_function = get_embedding_function()
    client = PersistentClient(path=cfg.db_dir_path)
    collection = client.get_or_create_collection(
        name=cfg.collection_name,
        embedding_function=embedding_function,  # pyright: ignore[reportArgumentType]
    )
    # 确保数据库中的文档完整，如果不完整则添加文档
    ensure_data_and_db(collection)

    # 启动RPC服务器
    server = ThreadedServer(RAGService(collection), hostname="localhost", port=cfg.port)
    end = time.time()
    logger.info(f"RAG RPC服务器已启动，用时{end - start:.2f}秒，监听 localhost:{cfg.port}")
    server.start()


if __name__ == "__main__":
    main()
