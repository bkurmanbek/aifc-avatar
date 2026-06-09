from __future__ import annotations

import atexit
import threading

from qdrant_client import QdrantClient
from qdrant_client.http import models

from .settings import QDRANT_COLLECTION, QDRANT_MODE, QDRANT_PATH, QDRANT_URL, QDRANT_VECTOR_SIZE

_CLIENT: QdrantClient | None = None
_CLIENT_LOCK = threading.Lock()


def client() -> QdrantClient:
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                if QDRANT_MODE == "local":
                    QDRANT_PATH.mkdir(parents=True, exist_ok=True)
                    _CLIENT = QdrantClient(
                        path=str(QDRANT_PATH),
                        force_disable_check_same_thread=True,
                    )
                else:
                    _CLIENT = QdrantClient(url=QDRANT_URL)
    return _CLIENT


def close_client() -> None:
    global _CLIENT
    if _CLIENT is None:
        return
    _CLIENT.close()
    _CLIENT = None


atexit.register(close_client)


def ensure_collection(recreate: bool = False) -> None:
    qdrant = client()
    exists = qdrant.collection_exists(QDRANT_COLLECTION)
    if exists and not recreate:
        return
    if exists:
        qdrant.delete_collection(QDRANT_COLLECTION)
    qdrant.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=models.VectorParams(
            size=QDRANT_VECTOR_SIZE,
            distance=models.Distance.COSINE,
        ),
    )
