import os
from pathlib import Path
from typing import Literal

from chromadb import Collection, Metadata, PersistentClient
from chromadb.utils.embedding_functions import (
    OpenAIEmbeddingFunction,
    SentenceTransformerEmbeddingFunction,
)
from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel, RootModel

load_dotenv(".env")
load_dotenv(".env.prod")


class RAGConfig(BaseModel):
    """RAG相关配置"""

    query_n_results: int = 3
    """查询返回的相关文档数量"""
    distance_threshold: float = 0.65
    """相关文档的距离阈值，数值越小表示越相关"""

    db_dir: str = "rag_db/"
    """向量数据库的存储目录名"""
    collection_name: str = "kanade_wiki_collection"
    """向量数据库中集合的名称"""
    document_file: str = "kanade_wiki.json"
    """初始文档文件名，位于插件配置目录下，格式为JSON，每个文档包含id、text和metadata字段"""

    embedding_type: Literal["sentence_transformer", "openai"] = "openai"
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
    openai_model: str = "BAAI/bge-m3"
    """OpenAI嵌入模型名称"""

    @property
    def db_dir_path(self) -> Path:
        """向量数据库的存储目录路径"""
        return Path("data") / "chat" / self.db_dir

    @property
    def document_file_path(self) -> Path:
        """初始文档文件的路径"""
        return Path("config") / "chat" / self.document_file


rag = RAGConfig(
    openai_api_key=os.getenv("CHAT__RAG__OPENAI_API_KEY"),
    openai_base_url=os.getenv("CHAT__RAG__OPENAI_BASE_URL"),
)


class Document(BaseModel):
    """一个文档对象"""

    id: str
    metadata: Metadata
    document: str


class Documents(RootModel[list[Document]]):
    pass


collection: Collection | None = None


def query(query_text: str) -> list[str] | None:
    """执行RAG查询，返回相似度分数低于阈值的相关文档列表"""
    if collection is None:
        logger.error("RAG数据库未初始化")
        return None

    results = collection.query(
        query_texts=query_text,
        n_results=rag.query_n_results,
    )

    documents = results["documents"]
    if documents is None:
        return None
    distances = results["distances"]
    if distances is None:
        return None

    filtered_docs = []
    for doc, dist in zip(documents[0], distances[0]):
        if dist < rag.distance_threshold:
            filtered_docs.append(doc)

    return filtered_docs


def _get_embedding_function():
    """根据配置获取合适的嵌入函数"""
    if rag.embedding_type == "openai":
        if not rag.openai_api_key and not os.getenv("CHROMA_OPENAI_API_KEY"):
            # 没有提供API密钥，也没有设置CHROMA_OPENAI_API_KEY
            raise ValueError("使用OpenAI嵌入时，必须提供openai_api_key")

        logger.info(
            f"使用OpenAI嵌入函数: model={rag.openai_model}, base_url={rag.openai_base_url or '默认'}"
        )
        return OpenAIEmbeddingFunction(
            api_key=rag.openai_api_key,
            model_name=rag.openai_model,
            api_base=rag.openai_base_url,
        )

    # 默认使用 Sentence Transformer
    logger.info(f"使用Sentence Transformer嵌入函数: model={rag.model_name_or_path}")
    return SentenceTransformerEmbeddingFunction(
        model_name=rag.model_name_or_path,
        normalize_embeddings=True,  # 可选：是否归一化向量，通常推荐在使用余弦相似度时开启
    )


def _ensure_data_and_db(collection: Collection):
    """预检文档数量，并确保数据库中的文档完整"""
    document_list = Documents.model_validate_json(
        rag.document_file_path.read_text(encoding="utf-8")
    ).root

    # 简单判定数据库中的文档是否完整，如果不完整则添加文档
    if collection.count() < len(document_list):
        logger.info("数据库中文档不完整，正在添加文档...")
        ids = [doc.id for doc in document_list]
        metadatas = [doc.metadata for doc in document_list]
        documents = [doc.document for doc in document_list]
        # 先删除可能存在的旧文档
        collection.delete(ids=ids)
        # 添加新文档
        collection.add(ids=ids, metadatas=metadatas, documents=documents)
        logger.info("文档添加完成。")


def startup():
    global collection
    embedding_function = _get_embedding_function()
    client = PersistentClient(path=rag.db_dir_path)
    collection = client.get_or_create_collection(
        name=rag.collection_name,
        embedding_function=embedding_function,  # pyright: ignore[reportArgumentType]
    )
    # 确保数据库中的文档完整，如果不完整则添加文档
    _ensure_data_and_db(collection)

    logger.info("RAG服务已启动。")


if __name__ == "__main__":
    startup()
    texts = query("宵崎奏生日啥时候")
    print(texts)
