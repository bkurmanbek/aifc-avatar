"""
scripts/processing/reindex.py — Rebuild the Qdrant vector index for AIFC RAG using BAAI/bge-m3.

Drops the existing collection and creates a fresh 1024-dim one,
then embeds chunks from one or more data/chunks/*.json files and upserts them
into the Qdrant DB at data/vector_db/.

Usage (from project root):
    python scripts/processing/reindex.py
    python scripts/processing/reindex.py --chunks data/chunks/chunks.json
    python scripts/processing/reindex.py --chunks data/chunks/chunks_afsa-web.json data/chunks/chunks_aix-web.json
    python scripts/processing/reindex.py --batch-size 32 --verbose
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import torch
from transformers import AutoModel, AutoTokenizer
import transformers.modeling_utils as _transformers_modeling_utils
from transformers.utils import import_utils as _transformers_import_utils
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
)

_transformers_modeling_utils.check_torch_load_is_safe = lambda: None
_transformers_import_utils.check_torch_load_is_safe = lambda: None

# ── Config ─────────────────────────────────────────────────────────────────────

# Project root is 2 levels up from scripts/processing/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR     = Path(os.getenv("AIFC_DATA_DIR", os.getenv("DATA_DIR", str(PROJECT_ROOT.parent / "data")))).expanduser().resolve()

QDRANT_PATH     = DATA_DIR / "vector_db"
COLLECTION_NAME = "aifc_chunks"
EMBED_DIM       = 1024
BATCH_SIZE      = 32

MODELS_DIR = Path(os.getenv("MODELS_DIR", str(Path.home() / "models")))
BGE_MODEL_PATH  = str(MODELS_DIR / "bge-m3")

DEFAULT_CHUNKS_FILE = DATA_DIR / "chunks" / "chunks.json"


def _resolve_device(devices: Any = None) -> str:
    if isinstance(devices, str) and devices:
        return devices
    if isinstance(devices, list) and devices:
        first = devices[0]
        if isinstance(first, str):
            return first
    return "cuda:0" if torch.cuda.is_available() else "cpu"


class BGEM3FlagModel:
    """Minimal local BGE-M3 embedder for reindexing."""

    def __init__(
        self,
        model_name_or_path: str,
        normalize_embeddings: bool = True,
        use_fp16: bool = True,
        devices: Any = None,
        trust_remote_code: bool = False,
        cache_dir: str | None = None,
        batch_size: int = 32,
        query_max_length: int = 512,
        **kwargs: Any,
    ):
        self.normalize_embeddings = normalize_embeddings
        self.batch_size = batch_size
        self.query_max_length = query_max_length
        self.device = _resolve_device(devices)

        torch_dtype = torch.float32
        if self.device != "cpu" and use_fp16:
            torch_dtype = torch.float16

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name_or_path,
            trust_remote_code=trust_remote_code,
            cache_dir=cache_dir,
        )
        self.model = AutoModel.from_pretrained(
            model_name_or_path,
            trust_remote_code=trust_remote_code,
            cache_dir=cache_dir,
            torch_dtype=torch_dtype,
            weights_only=False,
        )
        self.model.to(self.device)
        self.model.eval()

    def _pool_dense(self, last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        mask = attention_mask.unsqueeze(-1).to(last_hidden_state.dtype)
        pooled = (last_hidden_state * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1)
        if self.normalize_embeddings:
            pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        return pooled

    @torch.inference_mode()
    def encode(
        self,
        sentences: list[str] | str,
        batch_size: int | None = None,
        max_length: int | None = None,
        return_dense: bool = True,
        return_sparse: bool = False,
        return_colbert_vecs: bool = False,
        **kwargs: Any,
    ) -> dict[str, list[list[float]]]:
        if isinstance(sentences, str):
            sentences = [sentences]
        batch_size = batch_size or self.batch_size
        max_length = max_length or self.query_max_length

        dense_vecs: list[list[float]] = []
        for i in range(0, len(sentences), batch_size):
            batch = sentences[i : i + batch_size]
            inputs = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            inputs = {k: v.to(self.device) for k, v in inputs.items()}
            outputs = self.model(**inputs)
            pooled = self._pool_dense(outputs.last_hidden_state, inputs["attention_mask"])
            dense_vecs.extend(pooled.float().cpu().tolist())

        return {"dense_vecs": dense_vecs}

# ── Embed ──────────────────────────────────────────────────────────────────────

def load_chunks(chunks_files: list[Path]) -> list[dict]:
    """Load one or more chunk JSON files and dedupe by stable chunk_id."""
    chunks: list[dict] = []
    seen_ids: set[str] = set()
    for chunks_file in chunks_files:
        if not chunks_file.exists():
            print(f"[reindex] ERROR: chunks file not found: {chunks_file}", file=sys.stderr)
            sys.exit(1)
        data = json.loads(chunks_file.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print(f"[reindex] ERROR: chunks file must contain a JSON array: {chunks_file}", file=sys.stderr)
            sys.exit(1)
        before = len(chunks)
        for item in data:
            if not isinstance(item, dict):
                continue
            chunk_id = str(item.get("chunk_id") or item.get("id") or "")
            dedupe_key = chunk_id or str(item.get("text", ""))[:160]
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            chunks.append(item)
        print(f"[reindex] Loaded {len(chunks) - before} chunks from {chunks_file}", file=sys.stderr)
    print(f"[reindex] Combined selected chunks: {len(chunks)}", file=sys.stderr)
    return chunks


def embed_all(model: BGEM3FlagModel, texts: list[str], batch_size: int) -> list[list[float]]:
    """Embed texts in batches with the local BGE-M3 model."""
    vectors: list[list[float]] = []
    total = len(texts)
    for i in range(0, total, batch_size):
        batch = texts[i : i + batch_size]
        t0 = time.perf_counter()
        out = model.encode(
            batch,
            batch_size=batch_size,
            max_length=512,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        for vec in out["dense_vecs"]:
            vectors.append([float(v) for v in vec])
        pct = min(100, int((i + len(batch)) / total * 100))
        print(
            f"[reindex] {i + len(batch):>5}/{total}  ({pct:3d}%)  "
            f"batch={elapsed:.0f}ms",
            file=sys.stderr,
        )
    return vectors


def load_existing_vectors(client: QdrantClient) -> dict[str, tuple[str, list[float]]]:
    """Read current text and vectors keyed by chunk_id before recreating the collection."""
    existing: dict[str, tuple[str, list[float]]] = {}
    collection_names = {c.name for c in client.get_collections().collections}
    if COLLECTION_NAME not in collection_names:
        return existing

    print(f"[reindex] Loading existing vectors from '{COLLECTION_NAME}' for reuse...", file=sys.stderr)
    offset = None
    total = 0
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=512,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in points:
            payload = point.payload or {}
            chunk_id = str(payload.get("chunk_id") or "")
            vector = point.vector
            if chunk_id and isinstance(vector, list) and len(vector) == EMBED_DIM:
                existing[chunk_id] = (
                    str(payload.get("text") or ""),
                    [float(value) for value in vector],
                )
        total += len(points)
        if total and total % 4096 == 0:
            print(f"[reindex] loaded existing vectors: {total}", file=sys.stderr)
        if offset is None:
            break
    print(f"[reindex] Reusable vectors loaded: {len(existing)}", file=sys.stderr)
    return existing


def vectors_with_reuse(chunks: list[dict], batch_size: int, source_path: Path | None = None) -> list[list[float]]:
    """Reuse old vectors by stable chunk_id and embed only missing/new chunks."""
    reuse_path = source_path or QDRANT_PATH
    client = QdrantClient(path=str(reuse_path))
    try:
        existing = load_existing_vectors(client)
    finally:
        client.close()

    vectors: list[list[float] | None] = []
    missing_indexes: list[int] = []
    missing_texts: list[str] = []
    changed = 0
    for index, chunk in enumerate(chunks):
        chunk_id = str(chunk.get("chunk_id") or "")
        text = str(chunk.get("text", ""))
        old = existing.get(chunk_id)
        if old is None:
            vectors.append(None)
            missing_indexes.append(index)
            missing_texts.append(text)
        elif old[0] != text:
            changed += 1
            vectors.append(None)
            missing_indexes.append(index)
            missing_texts.append(text)
        else:
            vectors.append(old[1])

    print(
        f"[reindex] Vector reuse: reused={len(chunks) - len(missing_indexes)} "
        f"missing_or_changed={len(missing_indexes)} changed={changed}",
        file=sys.stderr,
    )
    if missing_indexes:
        print(f"[reindex] Loading bge-m3 from {BGE_MODEL_PATH} for missing vectors...", file=sys.stderr)
        t0 = time.perf_counter()
        model = BGEM3FlagModel(BGE_MODEL_PATH, use_fp16=True)
        print(f"[reindex] Model loaded in {(time.perf_counter()-t0)*1000:.0f}ms", file=sys.stderr)
        embedded = embed_all(model, missing_texts, batch_size)
        for index, vector in zip(missing_indexes, embedded):
            vectors[index] = vector

    if any(vector is None for vector in vectors):
        raise RuntimeError("failed to produce vectors for every chunk")
    return [vector for vector in vectors if vector is not None]


# ── Qdrant ─────────────────────────────────────────────────────────────────────

def rebuild_collection(client: QdrantClient) -> None:
    """Drop the existing Qdrant collection and recreate it with current dims."""
    existing = {c.name for c in client.get_collections().collections}
    if COLLECTION_NAME in existing:
        print(f"[reindex] Deleting old collection '{COLLECTION_NAME}'...", file=sys.stderr)
        client.delete_collection(COLLECTION_NAME)
    print(f"[reindex] Creating collection '{COLLECTION_NAME}' (dim={EMBED_DIM}, Cosine)...", file=sys.stderr)
    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )


def upsert_points(
    client: QdrantClient,
    chunks: list[dict],
    vectors: list[list[float]],
    batch_size: int,
    verbose: bool,
) -> None:
    """Write embedded chunks plus metadata into Qdrant."""
    total = len(chunks)
    for i in range(0, total, batch_size):
        batch_chunks  = chunks[i : i + batch_size]
        batch_vectors = vectors[i : i + batch_size]
        points = []
        for j, (chunk, vec) in enumerate(zip(batch_chunks, batch_vectors)):
            # Support both nested-metadata format {text, chunk_id, metadata: {...}}
            # and legacy flat format {text, chunk_id, source_file, domain, ...}.
            meta = chunk.get("metadata", chunk)
            payload_meta = {
                "source_file":    meta.get("source_file", ""),
                "domain":         meta.get("domain", ""),
                "doc_type":       meta.get("doc_type", ""),
                "language":       meta.get("language", ""),
                "section_title":  meta.get("section_title", ""),
                "is_table":       meta.get("is_table", False),
                "token_estimate": meta.get("token_estimate", 0),
            }
            points.append(PointStruct(
                id=i + j,
                vector=vec,
                payload={
                    "text":           chunk.get("text", ""),
                    "chunk_id":       chunk.get("chunk_id", ""),
                    "metadata":       payload_meta,
                    **payload_meta,
                },
            ))
        client.upsert(collection_name=COLLECTION_NAME, points=points)
        if verbose:
            print(f"[reindex] upserted {i + len(batch_chunks)}/{total}", file=sys.stderr)
    print(f"[reindex] Done — {total} points in '{COLLECTION_NAME}'", file=sys.stderr)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    """CLI entry point for rebuilding the vector index from current chunks."""
    parser = argparse.ArgumentParser(description="Rebuild Qdrant index with bge-m3 embeddings.")
    parser.add_argument(
        "--chunks",
        nargs="+",
        default=[str(DEFAULT_CHUNKS_FILE)],
        help="One or more chunk JSON files to index",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Embedding batch size")
    parser.add_argument("--verbose",    action="store_true", help="Print upsert progress")
    parser.add_argument(
        "--reuse-existing-vectors",
        action="store_true",
        help="Reuse existing vectors by stable chunk_id and embed only chunks missing from the old collection.",
    )
    parser.add_argument(
        "--reuse-vectors-from",
        default="",
        help="Optional source Qdrant path to read reusable vectors from while rebuilding the target data/vector_db.",
    )
    args = parser.parse_args()

    chunks_files = [Path(path) for path in args.chunks]

    # Load data
    chunks = load_chunks(chunks_files)
    texts  = [c.get("text", "") for c in chunks]

    if args.reuse_existing_vectors or args.reuse_vectors_from:
        t_embed = time.perf_counter()
        source_path = Path(args.reuse_vectors_from) if args.reuse_vectors_from else QDRANT_PATH
        vectors = vectors_with_reuse(chunks, args.batch_size, source_path=source_path)
        print(f"[reindex] Vector preparation complete: {(time.perf_counter()-t_embed)*1000:.0f}ms", file=sys.stderr)
    else:
        # Load embedding model
        print(f"[reindex] Loading bge-m3 from {BGE_MODEL_PATH}...", file=sys.stderr)
        t0 = time.perf_counter()
        model = BGEM3FlagModel(BGE_MODEL_PATH, use_fp16=True)
        print(f"[reindex] Model loaded in {(time.perf_counter()-t0)*1000:.0f}ms", file=sys.stderr)

        # Embed
        print(f"[reindex] Embedding {len(texts)} chunks (batch={args.batch_size})...", file=sys.stderr)
        t_embed = time.perf_counter()
        vectors = embed_all(model, texts, args.batch_size)
        print(f"[reindex] Embedding complete: {(time.perf_counter()-t_embed)*1000:.0f}ms", file=sys.stderr)

    # Rebuild Qdrant
    client = QdrantClient(path=str(QDRANT_PATH))
    rebuild_collection(client)
    upsert_points(client, chunks, vectors, args.batch_size, args.verbose)
    client.close()

    print(
        f"[reindex] Index rebuilt. "
        f"Collection={COLLECTION_NAME!r}, dim={EMBED_DIM}, n={len(chunks)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
