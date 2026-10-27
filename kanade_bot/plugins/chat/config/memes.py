"""表情包语义检索配置"""

from pathlib import Path

from nonebot import require
from pydantic import PositiveInt

from kanade_bot.utils.schema import AttrDocModel, BaseAgentConfig, ProviderConfig

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_config_file, get_plugin_data_file


class StickerTaggerConfig(BaseAgentConfig):
    """表情包视觉打标模型配置"""

    system_prompt_file: str = "StickerTagging.md"
    """系统提示词文件名"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件路径"""
        return get_plugin_config_file(self.system_prompt_file)


class StickerEmbeddingConfig(AttrDocModel):
    """表情包 embedding 模型配置"""

    model: str = ""
    """embedding 模型 ID，留空视为未启用"""

    provider: ProviderConfig | None = None
    """embedding API 提供商配置，None 时使用 OpenAI 官方端点"""


class StickerMemeConfig(AttrDocModel):
    """表情包语义检索配置

    基于向量检索的表情包系统：图片由视觉模型自动打标，
    LLM 以 `{{表情: 描述}}` 占位符引用，投递时按语义相似度加权随机选图。
    """

    enabled: bool = False
    """是否启用表情包语义检索；未启用时占位符将被静默丢弃"""

    memes_dir: str = "memes/"
    """表情包存储目录名"""

    tagger: StickerTaggerConfig | None = None
    """视觉打标模型配置，None 时无法自动打标"""

    embedding: StickerEmbeddingConfig = StickerEmbeddingConfig()
    """embedding 模型配置"""

    topk: PositiveInt = 5
    """检索候选数：TopK 内按相似度 softmax 加权随机抽取"""

    min_similarity: float = 0.3
    """最低余弦相似度，低于该值的候选不参与抽取"""

    softmax_scale: float = 55.0
    """相似度 softmax 温度系数，越大越偏向最高分"""

    index_file: str = "memes_index.json"
    """索引元数据文件名（json）"""

    matrix_file: str = "memes_index.npy"
    """向量矩阵文件名（npy）"""

    @property
    def memes_dir_path(self) -> Path:
        """表情包存储目录路径"""
        return get_plugin_data_file(self.memes_dir)

    @property
    def index_file_path(self) -> Path:
        """索引元数据文件路径"""
        return get_plugin_data_file(self.index_file)

    @property
    def matrix_file_path(self) -> Path:
        """向量矩阵文件路径"""
        return get_plugin_data_file(self.matrix_file)
