from __future__ import annotations

from .model_loader import reranker as load_reranker
from .settings import RAG_FINAL_TOP_K


def rerank(query: str, candidates: list[dict], top_n: int = RAG_FINAL_TOP_K) -> list[dict]:
    if not candidates:
        return []

    top_n = min(top_n, len(candidates))
    try:
        model = load_reranker()
        pairs = [[query, candidate["text"]] for candidate in candidates]
        scores = model.compute_score(pairs, normalize=True)
        scored = sorted(zip(scores, candidates), key=lambda item: item[0], reverse=True)
        return [
            {**candidate, "rerank_score": round(float(score), 4)}
            for score, candidate in scored[:top_n]
        ]
    except Exception:
        fallback = sorted(candidates, key=lambda item: item["ann_score"], reverse=True)[:top_n]
        return [{**candidate, "rerank_score": candidate["ann_score"]} for candidate in fallback]
