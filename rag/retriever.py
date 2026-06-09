from __future__ import annotations

from .model_loader import embedder
from .qdrant import client
from .settings import (
    EMBEDDING_BATCH_SIZE,
    QDRANT_COLLECTION,
    RAG_ANN_TOP_K,
    RAG_MIN_ANN_SCORE,
)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []

    vectors: list[list[float]] = []
    model = embedder()
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[start : start + EMBEDDING_BATCH_SIZE]
        encoded = model.encode(
            batch,
            batch_size=EMBEDDING_BATCH_SIZE,
            max_length=512,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        vectors.extend([[float(value) for value in vector] for vector in encoded["dense_vecs"]])
    return vectors


def embed_query(query: str) -> list[float]:
    return embed_texts([query])[0]


def retrieve_candidates(query: str, top_k: int = RAG_ANN_TOP_K) -> list[dict]:
    vector = embed_query(query)
    points = client().query_points(
        collection_name=QDRANT_COLLECTION,
        query=vector,
        limit=top_k,
    ).points

    candidates: list[dict] = []
    for point in points:
        if point.score < RAG_MIN_ANN_SCORE:
            continue
        payload = point.payload or {}
        candidates.append(
            {
                "text": payload.get("text", ""),
                "chunk_id": payload.get("chunk_id", ""),
                "metadata": payload.get("metadata", {}),
                "ann_score": round(float(point.score), 4),
            }
        )
    return candidates
