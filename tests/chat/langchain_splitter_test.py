from pathlib import Path

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
        ],
        strip_headers=False,
    )
    header_docs = header_splitter.split_text(md_text)

    # 第二阶段：针对中文长段落做递归切分，尽量在自然边界断开
    char_splitter = RecursiveCharacterTextSplitter(
        chunk_size=700,
        chunk_overlap=120,
        separators=[
            "\n### ",
            "\n## ",
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


if __name__ == "__main__":
    target = "./assets/Kanade-wiki.md"
    docs = split_kanade_wiki(target)

    print(f"总分块数: {len(docs)}")
    print("=" * 60)
    for doc in docs[:5]:
        h2 = doc.metadata.get("h2", "<no-h2>")
        preview = doc.page_content[:140].replace("\n", " ")
        print(f"chunk_id={doc.metadata['chunk_id']} | h2={h2}")
        print(preview)
        print("-" * 60)
