import argparse
import logging
import sys
from pathlib import Path

EMBEDDING_MODEL = "text-embedding-v1"
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def _collect_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    patterns = ("*.txt", "*.md", "*.markdown")
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(sorted(input_path.rglob(pat)))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Import UTF-8 text or Markdown into this project's Milvus.")
    parser.add_argument("input_path", type=Path, help="A text/Markdown file or a directory to import")
    parser.add_argument("--collection", help="Destination knowledge collection (defaults to MILVUS_KNOWLEDGE_COLLECTION)")
    args = parser.parse_args()
    input_path = args.input_path.expanduser().resolve()
    if not input_path.exists():
        parser.error(f"Input path does not exist: {input_path}")
    paths = _collect_paths(input_path)
    if not paths:
        parser.error(f"No text/Markdown files found: {input_path}")

    # app/ contains mult_agents; load dependencies only after CLI validation.
    app_root = Path(__file__).resolve().parents[2]
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    from mult_agents.config import AppConfig
    from mult_agents.rag.core import RAGConfig, RAGSystem

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    config = AppConfig.from_file(require_llm=False)
    if not config.embedding_api_key:
        parser.error("缺少 DASHSCOPE_API_KEY，请填写阿里云向量化密钥")
    collection_name = args.collection or config.knowledge_collection
    if collection_name == config.milvus_collection:
        parser.error("知识库集合不能与个人记忆集合相同")
    rag_cfg = RAGConfig(
        milvus_host=config.milvus_host,
        milvus_port=config.milvus_port,
        collection_name=collection_name,
        embedding_model=EMBEDDING_MODEL,
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    rag = RAGSystem(api_key=config.embedding_api_key, config=rag_cfg)

    total_chunks = rag.ingest_paths(paths)
    print(f"入库完成 | 文件数={len(paths)} | chunk数={total_chunks} | collection={collection_name}")


if __name__ == "__main__":
    main()
