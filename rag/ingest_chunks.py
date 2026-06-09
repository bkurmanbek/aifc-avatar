from __future__ import annotations

import argparse
import json
from uuid import uuid5, NAMESPACE_URL

from qdrant_client.http import models

from .qdrant import client, ensure_collection
from .retriever import embed_texts
from .settings import INGEST_BATCH_SIZE, QDRANT_COLLECTION, RAG_CHUNKS_PATH


def load_chunks(path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError("chunks file must be a JSON array")
    return data


def point_id(chunk: dict) -> str:
    raw_id = str(chunk.get("chunk_id") or chunk.get("id") or chunk.get("text", "")[:120])
    return str(uuid5(NAMESPACE_URL, raw_id))


def ingest(path=RAG_CHUNKS_PATH, recreate: bool = False) -> int:
    ensure_collection(recreate=recreate)
    qdrant = client()
    chunks = load_chunks(path)

    total = 0
    for start in range(0, len(chunks), INGEST_BATCH_SIZE):
        batch = chunks[start : start + INGEST_BATCH_SIZE]
        texts = [str(item.get("text", "")).strip() for item in batch]
        vectors = embed_texts(texts)
        points = [
            models.PointStruct(
                id=point_id(chunk),
                vector=vector,
                payload={
                    "chunk_id": chunk.get("chunk_id", ""),
                    "text": text,
                    "metadata": chunk.get("metadata", {}),
                },
            )
            for chunk, text, vector in zip(batch, texts, vectors)
            if text
        ]
        if points:
            qdrant.upsert(collection_name=QDRANT_COLLECTION, points=points)
            total += len(points)
            print(f"ingested {total}/{len(chunks)}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=str(RAG_CHUNKS_PATH))
    parser.add_argument("--recreate", action="store_true")
    args = parser.parse_args()
    total = ingest(args.path, recreate=args.recreate)
    print(f"done: {total} chunks")


if __name__ == "__main__":
    main()
