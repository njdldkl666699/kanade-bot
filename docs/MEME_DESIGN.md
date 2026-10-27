# 表情包语义检索设计

## 目标与边界

让模型像人一样"在合适的时机发合适的表情包"。旧协议是类别目录加模型点名：`chat_configs.json` 维护类别清单，模型输出 `{{类别名}}` 占位符，投递时在该类别目录内随机选图——检索粒度粗（类别而非单张）、清单需要人工维护、模型必须预知库存。

新协议改为语义检索：每张表情包由视觉模型自动打标，标签拼接文本的向量即为该图的语义表示；模型输出 `{{表情: 自然语言描述}}` 占位符，投递时按描述向量检索最匹配的一张。模型无需预知库存，收录即生效。思路参考 StickerSelector（`cache/misc_not_plugin/StickerSelector`）的本地化实现，交互协议沿用项目原有的占位符风格。

## 数据模型与存储

目录即真相，索引可随时从目录重建：

| 路径                                  | 内容                                                       |
| ------------------------------------- | ---------------------------------------------------------- |
| `data/chat/memes/`                    | 扁平图片目录，文件名为 `{uuid}.{ext}`                      |
| `data/chat/memes_index.json`          | 元数据：`meta`（embedding 模型名、更新时间）与 `records`    |
| `data/chat/memes_index.npy`           | `float32[N, D]` 向量矩阵，第 i 行对应 `records[i]`          |

json 中的每条记录包含 `id`（自增，供命令引用）、`file`、`tags`、`needs_tag`、`added_at`，不含向量。向量以 numpy 原生格式存矩阵文件，读写零解析；标签部分保持人类可读、可直接修正。

可靠性约定：

- 写入顺序为先 `.npy` 后 `.json`（各自 tmp + replace 原子替换）。中途崩溃时两者行数不一致，下次加载检测到后清空向量并置重建标记，由启动流程自动批量重算恢复。
- 矩阵中全零行表示该记录无向量，加载时回填为空列表；零向量余弦得分为 0，天然被相似度阈值过滤。
- 加载时对比 json `meta.embedding_model` 与当前配置的模型，不一致则向量视为失效并触发全量重建——不同模型的向量空间与维度互不兼容。
- 旧格式（json 内嵌 `embedding` 字段）在加载时自动转存为双文件格式。

## 录入与打标

收录入口是 `添加表情` 命令（引用一张图片），流程：取引用图片 → 复制进扁平目录（uuid 重命名）→ 视觉模型打标 → 标签拼接文本调 embedding API → 写入索引。命令参数作为人工补充标签与自动打标结果合并。

打标模型由 `chat.sticker_meme.tagger` 独立配置（`StickerTaggerConfig`，继承通用 agent 配置并附带 `system_prompt_file` 提示词文件，与图片转述 `chat.image_caption` 互不依赖），使用 pydantic-ai Agent + BinaryContent。提示词文件默认 `config/chat/StickerTagging.md`，缺失时回退 `memes/index.py` 内置默认。提示词要求输出 5~10 个中文标签，覆盖画面主体、情绪、动作、图内文字的语义、适用场景五个维度；标签定位是"检索关键词"，即群友想发这张图时会用的口语化说法。输出解析宽容提取首个 JSON 数组，失败重试一次。

打标始终失败时图片以 `needs_tag` 状态入库（保留人工补充标签，若有），不参与检索，事后用 `改标` 命令补标。

存量迁移脚本 `scripts/migrate_memes`：遍历旧类别目录，逐图打标（旧类别描述并入标签，保留人工语义），写入新索引；`--dry-run` 仅打标预览，`--apply` 额外从 `chat_configs.json` 移除旧 `memes` 字段。旧类别目录保留作备份。

## 检索与发送

`select(query)`：查询文本 → embedding → 与内存矩阵点积（双方 L2 归一化，点积即余弦）→ 过滤待打标记录与低于 `min_similarity` 的候选 → TopK 内按 `softmax(score × softmax_scale)` 加权随机抽取一张。加权随机而非取最高分，避免同一描述永远命中同一张图。

发送协议在投递层（`deliver.py`）解析：

- 模型输出 `{{表情: 描述}}`，描述为任意自然语言，必须作为独立消息（前后两个换行）。
- 检索无结果、功能未启用或检索异常时静默丢弃占位符，只发文本。
- 命中后以动画表情消息段（`OneBotMessageSegmentMeme`）发送。

模型侧不注入库存清单。使用说明位于 `config/chat/prompts/memes.md`，是会话层条件片段（`when=memes_enabled`），功能未启用时模型完全不知道表情包的存在；原 `list_memes` 工具已随旧协议移除。

## 命令

| 命令                    | 别名                     | 权限       | 说明                                 |
| ----------------------- | ------------------------ | ---------- | ------------------------------------ |
| `添加表情 [补充标签…]`  | `add_meme` `addmeme`     | SUPERUSER  | 引用图片收录，视觉自动打标           |
| `表情包列表`            | `list_memes` `listmemes` | -          | 列出序号、文件名与标签               |
| `改标 <序号> <标签…>`   | `retag_meme` `retagmeme` | SUPERUSER  | 更新标签并重算向量                   |
| `删除表情 <序号>`       | `del_meme` `delmeme`     | SUPERUSER  | 删除记录与图片文件                   |
| `扫描表情`              | `scan_memes` `scanmemes` | SUPERUSER  | 收录手动放入目录的图片并清理悬空记录 |

## 模块组织

核心 `kanade_bot/plugins/chat/memes/index.py` 是纯逻辑模块：只依赖 numpy 与 openai，不依赖 nonebot。运行时由 `chat/memes/__init__.py` 在启动时按配置装配实例（`get_memes()` 获取，未启用返回 None），模型不符或行数不符时自动触发重建。

由于 `chat/__init__.py` 的导入链依赖已初始化的 nonebot，独立脚本与测试无法直接按包名导入该模块；`scripts/migrate_memes.py` 与 `tests/chat/meme_index_test.py` 使用 `importlib` 按文件路径 spec 加载，并预注册 `sys.modules` 使后续导入命中缓存而不触发包初始化（与 `test_prompt_sections.py` 的做法一致）。

## 配置

```yaml
chat:
  sticker_meme:
    enabled: true
    memes_dir: memes/            # 表情包存储目录（相对插件数据目录）
    index_file: memes_index.json   # 索引元数据文件
    matrix_file: memes_index.npy   # 向量矩阵文件
    tagger:                    # 视觉打标模型（StickerTaggerConfig，继承 BaseAgentConfig）
      model: sensenova-6.8-flash-lite
      provider: { base_url: xxx, api_key: xxx }
      vision: true
      system_prompt_file: StickerTagging.md
    embedding:                 # embedding 模型（OpenAI 兼容端点）
      model: BAAI/bge-m3
      provider: { base_url: xxx, api_key: xxx }
    topk: 5                     # 候选数
    min_similarity: 0.3         # 最低余弦相似度
    softmax_scale: 55.0         # 加权随机温度，越大越偏向最高分
```

视觉打标与 embedding 分别由 `tagger`、`embedding` 子配置独立指定；`tagger` 未配置时收录以待打标状态入库、迁移脚本拒绝运行。图片转述 `chat.image_caption` 与此无关。

## 已知限制与后续方向

- 检索为全量暴力矩阵点积，万张以内无压力；规模显著增长后再考虑 ANN。
- 无使用统计与冷却机制，理论上模型可高频发送同一张图；后续可按记录统计命中次数并反馈到抽取权重。
- 打标质量依赖视觉模型，错误的标签会直接导致检索偏差，依赖 `改标` 人工修正；可考虑投递时把命中标签写回会话通知，让模型感知库存风格。
- 动图（gif）仅按首页帧打标。
