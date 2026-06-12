from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from .settings import FAISS_INDEX_PATH, FAISS_METADATA_PATH, RAG_MIN_ANN_SCORE

_INDEX = None
_METADATA: list[dict] | None = None
_LOCK = threading.Lock()


def _faiss():
    try:
        import faiss  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "FAISS backend selected but faiss-cpu is not installed. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc
    return faiss


def _load(index_path: Path = FAISS_INDEX_PATH, metadata_path: Path = FAISS_METADATA_PATH):
    global _INDEX, _METADATA
    if _INDEX is not None and _METADATA is not None:
        return _INDEX, _METADATA
    with _LOCK:
        if _INDEX is not None and _METADATA is not None:
            return _INDEX, _METADATA
        if not index_path.exists() or not metadata_path.exists():
            raise RuntimeError(
                f"FAISS index is missing. Build it with: python -m scripts.build_faiss_index "
                f"(expected {index_path} and {metadata_path})"
            )
        faiss = _faiss()
        index = faiss.read_index(str(index_path))
        with metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        if not isinstance(metadata, list):
            raise RuntimeError(f"FAISS metadata must be a JSON array: {metadata_path}")
        if len(metadata) < index.ntotal:
            raise RuntimeError(
                f"FAISS metadata has {len(metadata)} rows but index has {index.ntotal} vectors"
            )
        _INDEX = index
        _METADATA = metadata
        return _INDEX, _METADATA


def reset_cache() -> None:
    global _INDEX, _METADATA
    with _LOCK:
        _INDEX = None
        _METADATA = None


def search(vector: list[float], top_k: int, min_score: float = RAG_MIN_ANN_SCORE) -> list[dict]:
    index, metadata = _load()
    faiss = _faiss()
    query = np.asarray([vector], dtype=np.float32)
    faiss.normalize_L2(query)
    scores, ids = index.search(query, top_k)

    candidates: list[dict] = []
    for score, idx in zip(scores[0], ids[0]):
        if idx < 0:
            continue
        score_value = float(score)
        if score_value < min_score:
            continue
        payload = metadata[int(idx)] or {}
        candidates.append(
            {
                "text": payload.get("text", ""),
                "chunk_id": payload.get("chunk_id", ""),
                "metadata": payload.get("metadata", {}),
                "ann_score": round(score_value, 4),
            }
        )
    return candidates
