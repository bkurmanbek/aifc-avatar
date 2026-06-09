from __future__ import annotations

from .model_loader import embedder
from .settings import (
    EMBEDDING_BATCH_SIZE,
    QDRANT_COLLECTION,
    RAG_ANN_TOP_K,
    RAG_MIN_ANN_SCORE,
    RAG_VECTOR_BACKEND,
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


def _retrieve_qdrant(vector: list[float], top_k: int) -> list[dict]:
    from .qdrant import client

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


def retrieve_candidates(query: str, top_k: int = RAG_ANN_TOP_K) -> list[dict]:
    vector = embed_query(query)
    if RAG_VECTOR_BACKEND == "faiss":
        from .faiss_store import search

        return search(vector, top_k=top_k, min_score=RAG_MIN_ANN_SCORE)
    if RAG_VECTOR_BACKEND == "qdrant":
        return _retrieve_qdrant(vector, top_k=top_k)
    raise RuntimeError(f"Unsupported RAG_VECTOR_BACKEND={RAG_VECTOR_BACKEND!r}")
