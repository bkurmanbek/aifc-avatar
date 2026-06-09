from __future__ import annotations

from .reranker import rerank
from .retriever import retrieve_candidates
from .settings import RAG_ANN_TOP_K, RAG_FINAL_TOP_K


def retrieve(query: str, top_ann: int = RAG_ANN_TOP_K, top_final: int = RAG_FINAL_TOP_K) -> list[dict]:
    candidates = retrieve_candidates(query, top_k=top_ann)
    return rerank(query, candidates, top_n=top_final)


async def aretrieve(query: str, top_ann: int = RAG_ANN_TOP_K, top_final: int = RAG_FINAL_TOP_K) -> list[dict]:
    return retrieve(query, top_ann=top_ann, top_final=top_final)


def format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No retrieved context."
    blocks: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk.get("metadata") or {}
        title = metadata.get("section_title") or metadata.get("source_file") or chunk.get("chunk_id")
        blocks.append(f"[{index}] {title}\n{chunk.get('text', '').strip()}")
    return "\n\n".join(blocks)
