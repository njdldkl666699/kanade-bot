import time

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

MODEL_NAME_OR_PATH = "bge-small-zh-v1.5"
"""模型名称或路径"""

PERSISTENT_PATH = "kanade_rag_db"
"""向量数据库的持久化路径"""

COLLECTION_NAME = "kanade_wiki_collection"
"""向量数据库中集合的名称"""

# 1. 创建 BGE 小型中文模型的嵌入函数
#    模型会自动从 Hugging Face 下载到本地
bge_ef = SentenceTransformerEmbeddingFunction(
    model_name=MODEL_NAME_OR_PATH, normalize_embeddings=True
)

# 2. 初始化 ChromaDB 客户端（使用持久化目录）
client = chromadb.PersistentClient(path=PERSISTENT_PATH)

# 3. 创建集合，并指定我们上面定义的嵌入函数
collection = client.get_or_create_collection(
    name=COLLECTION_NAME,
    embedding_function=bge_ef,  # pyright: ignore[reportArgumentType]
)

# 6. 执行检索测试
query = "充电宝能带到电梯里吗"
start = time.time()
results = collection.query(
    query_texts=[query],
    n_results=3,  # 返回最相关的3个结果
)
end = time.time()

print(f"\n查询耗时: {end - start:.4f} 秒")
print(f"\n查询问题: {query}")
print(f"最相关的文档是: \n{results['documents']}")
print(f"相似度分数: \n{results['distances']}")
