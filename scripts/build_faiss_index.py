from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

from rag.retriever import embed_texts
from rag.settings import (
    EMBEDDING_BATCH_SIZE,
    FAISS_INDEX_PATH,
    FAISS_METADATA_PATH,
    QDRANT_VECTOR_SIZE,
    RAG_CHUNKS_PATH,
)


def _faiss():
    try:
        import faiss  # type: ignore
    except Exception as exc:
        raise SystemExit(
            "faiss-cpu is required. Install dependencies with: pip install -r requirements.txt"
        ) from exc
    return faiss


def load_chunks(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"chunks file must contain a JSON array: {path}")
    return data


def metadata_for(chunk: dict, text: str) -> dict:
    return {
        "text": text,
        "chunk_id": chunk.get("chunk_id") or chunk.get("id") or "",
        "metadata": chunk.get("metadata", {}),
    }


def build_index(
    chunks_path: Path,
    index_path: Path,
    metadata_path: Path,
    batch_size: int,
) -> int:
    faiss = _faiss()
    chunks = load_chunks(chunks_path)
    index = faiss.IndexFlatIP(QDRANT_VECTOR_SIZE)
    metadata: list[dict] = []

    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        texts = [str(item.get("text", "")).strip() for item in batch]
        kept = [(chunk, text) for chunk, text in zip(batch, texts) if text]
        if not kept:
            continue
        vectors = np.asarray(embed_texts([text for _, text in kept]), dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != QDRANT_VECTOR_SIZE:
            raise RuntimeError(
                f"expected vectors with dim {QDRANT_VECTOR_SIZE}, got shape {vectors.shape}"
            )
        faiss.normalize_L2(vectors)
        index.add(vectors)
        metadata.extend(metadata_for(chunk, text) for chunk, text in kept)
        print(f"indexed {len(metadata)}/{len(chunks)}")

    index_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_path))
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False)
    return len(metadata)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", type=Path, default=RAG_CHUNKS_PATH)
    parser.add_argument("--index", type=Path, default=FAISS_INDEX_PATH)
    parser.add_argument("--metadata", type=Path, default=FAISS_METADATA_PATH)
    parser.add_argument("--batch-size", type=int, default=EMBEDDING_BATCH_SIZE)
    args = parser.parse_args()

    total = build_index(
        chunks_path=args.chunks.expanduser().resolve(),
        index_path=args.index.expanduser().resolve(),
        metadata_path=args.metadata.expanduser().resolve(),
        batch_size=max(1, args.batch_size),
    )
    print(f"done: {total} vectors -> {args.index}")


if __name__ == "__main__":
    main()
