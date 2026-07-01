from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceBgeEmbeddings
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter


def split_kanade_wiki(markdown_path: str):
    """对 Kanade wiki 做两阶段切分：标题分段 + 中文语义递归切分。"""
    md_text = Path(markdown_path).read_text(encoding="utf-8")

    # 第一阶段：保留文档结构，按标题切出主题块
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[
            ("#", "h1"),
            ("##", "h2"),
            ("###", "h3"),
            ("####", "h4"),
        ],
        strip_headers=True,  # 保留标题文本在内容中，方便后续检索时上下文理解
    )
    header_docs = header_splitter.split_text(md_text)

    # 第二阶段：针对中文长段落做递归切分，尽量在自然边界断开
    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=300,
        chunk_overlap=100,
        separators=[
            "\n\n",
            "\n",
            "。",
            "！",
            "？",
            "；",
            "，",
            " ",
            "",
        ],
    )

    chunks = char_splitter.split_documents(header_docs)

    # 增加可追溯元信息，后续写入向量库时方便定位
    for i, doc in enumerate(chunks):
        doc.metadata["source"] = markdown_path
        doc.metadata["chunk_id"] = i

    return chunks


# 1. 创建 BGE 小型中文模型的嵌入函数
embeddings = HuggingFaceBgeEmbeddings(
    model_name="./bge-small-zh-v1.5",
    model_kwargs={"device": "cpu"},  # 根据你的环境调整设备
    encode_kwargs={
        "normalize_embeddings": True
    },  # 可选：是否归一化向量，通常推荐在使用余弦相似度时开启
)

documents = split_kanade_wiki("./assets/Kanade-wiki.md")

# 2. 连接到你的 Chroma 数据库（指定持久化目录）
# 判断数据库是否已存在，如果不存在则创建并添加文档；如果存在则直接连接
vector_store = Chroma.from_documents(
    documents,
    embedding=embeddings,
    collection_name="kanade_wiki_collection",
    persist_directory="./my_rag_db",
)
# vector_store = Chroma(
#     collection_name="kanade_wiki_collection",
#     embedding_function=embeddings,
#     persist_directory="./my_rag_db",
# )

# 3. 直接使用 LangChain 的方法进行检索
results = vector_store.similarity_search_with_score("穗波今天给你做了什么", k=3)

for doc, score in results:
    print(f"相关度分数: {score}")  # 注意：这里的分数是距离，越小越相关
    print(f"文档内容: {doc.page_content}\n")
