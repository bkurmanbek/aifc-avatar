from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(
    os.getenv("AIFC_DATA_DIR", os.getenv("DATA_DIR", str(PROJECT_ROOT.parent / "data")))
).expanduser().resolve()


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_PATH = Path(os.getenv("QDRANT_PATH", str(DATA_DIR / "vector_db"))).expanduser().resolve()
QDRANT_MODE = os.getenv("QDRANT_MODE", "local").lower()
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "aifc_chunks")
QDRANT_VECTOR_SIZE = env_int("QDRANT_VECTOR_SIZE", 1024)
RAG_VECTOR_BACKEND = os.getenv("RAG_VECTOR_BACKEND", "faiss").strip().lower()
FAISS_INDEX_PATH = Path(os.getenv("FAISS_INDEX_PATH", str(DATA_DIR / "faiss" / "aifc_chunks.index"))).expanduser().resolve()
FAISS_METADATA_PATH = Path(
    os.getenv("FAISS_METADATA_PATH", str(DATA_DIR / "faiss" / "aifc_chunks.metadata.json"))
).expanduser().resolve()
RAG_ANN_TOP_K = env_int("RAG_ANN_TOP_K", 20)
RAG_FINAL_TOP_K = env_int("RAG_FINAL_TOP_K", 5)
RAG_MIN_ANN_SCORE = env_float("RAG_MIN_ANN_SCORE", 0.50)
_configured_chunks_path = Path(os.getenv("RAG_CHUNKS_PATH", str(DATA_DIR / "chunks" / "chunks.json"))).expanduser().resolve()
_local_chunks_path = DATA_DIR / "chunks" / "chunks.json"
RAG_CHUNKS_PATH = (
    _configured_chunks_path
    if _configured_chunks_path.exists()
    else _local_chunks_path.resolve()
)

_models_dir = Path(os.getenv("MODELS_DIR", str(Path.home() / "models"))).resolve()
if not _models_dir.exists():
    _models_dir = Path.home() / "models"


def _existing_model_path(env_name: str, default: Path) -> str:
    configured = Path(os.getenv(env_name, str(default))).resolve()
    if configured.exists():
        return str(configured)
    return str(default)


EMBEDDING_MODEL_PATH = _existing_model_path("EMBEDDING_MODEL_PATH", _models_dir / "bge-m3")
RERANKER_MODEL_PATH = _existing_model_path("RERANKER_MODEL_PATH", _models_dir / "bge-reranker-v2-m3")
EMBEDDING_BATCH_SIZE = env_int("EMBEDDING_BATCH_SIZE", 64)
INGEST_BATCH_SIZE = env_int("INGEST_BATCH_SIZE", 64)
