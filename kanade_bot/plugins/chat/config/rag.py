"""RAG 检索配置"""

from pathlib import Path
from typing import Literal

from nonebot import require

from kanade_bot.utils.schema import AttrDocModel

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_config_file, get_plugin_data_file


class RAGConfig(AttrDocModel):
    """RAG相关配置"""

    enabled: bool = False
    """是否启用RAG功能"""
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

    @property
    def db_dir_path(self) -> Path:
        """向量数据库的存储目录路径"""
        return get_plugin_data_file(self.db_dir)

    @property
    def document_file_path(self) -> Path:
        """初始文档文件的路径"""
        return get_plugin_config_file(self.document_file)
