from __future__ import annotations

from typing import Any

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from scripts.reindex import BGEM3FlagModel

from .settings import EMBEDDING_MODEL_PATH, RERANKER_MODEL_PATH


def _resolve_device(devices: Any = None) -> str:
    if isinstance(devices, str) and devices:
        return devices
    if isinstance(devices, list) and devices:
        first = devices[0]
        if isinstance(first, str):
            return first
    return "cuda:0" if torch.cuda.is_available() else "cpu"


class FlagReranker:
    """Minimal local cross-encoder reranker."""

    def __init__(
        self,
        model_name_or_path: str,
        use_fp16: bool = False,
        trust_remote_code: bool = False,
        cache_dir: str | None = None,
        devices: Any = None,
        batch_size: int = 128,
        max_length: int = 512,
        normalize: bool = False,
        **_: Any,
    ) -> None:
        self.batch_size = batch_size
        self.max_length = max_length
        self.normalize = normalize
        self.device = _resolve_device(devices)
        torch_dtype = torch.float16 if self.device != "cpu" and use_fp16 else torch.float32
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name_or_path,
            trust_remote_code=trust_remote_code,
            cache_dir=cache_dir,
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_name_or_path,
            trust_remote_code=trust_remote_code,
            cache_dir=cache_dir,
            torch_dtype=torch_dtype,
            weights_only=False,
        )
        self.model.to(self.device)
        self.model.eval()

    @torch.inference_mode()
    def compute_score(
        self,
        sentence_pairs: list[list[str]] | list[tuple[str, str]],
        batch_size: int | None = None,
        max_length: int | None = None,
        normalize: bool | None = None,
        **_: Any,
    ) -> list[float]:
        if not sentence_pairs:
            return []
        if isinstance(sentence_pairs[0], str):
            sentence_pairs = [sentence_pairs]  # type: ignore[list-item]
        batch_size = batch_size or self.batch_size
        max_length = max_length or self.max_length
        normalize = self.normalize if normalize is None else normalize
        scores: list[float] = []
        for start in range(0, len(sentence_pairs), batch_size):
            batch = sentence_pairs[start : start + batch_size]
            queries = [pair[0] for pair in batch]
            passages = [pair[1] for pair in batch]
            inputs = self.tokenizer(
                queries,
                passages,
                padding=True,
                truncation=True,
                max_length=max_length,
                return_tensors="pt",
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            logits = self.model(**inputs).logits
            if logits.ndim == 2 and logits.shape[-1] > 1:
                batch_scores = logits[:, -1]
            else:
                batch_scores = logits.squeeze(-1)
            if normalize:
                batch_scores = torch.sigmoid(batch_scores)
            scores.extend(batch_scores.float().cpu().tolist())
        return scores


_embedder: BGEM3FlagModel | None = None
_reranker: FlagReranker | None = None


def embedder() -> BGEM3FlagModel:
    global _embedder
    if _embedder is None:
        _embedder = BGEM3FlagModel(EMBEDDING_MODEL_PATH, use_fp16=True)
    return _embedder


def reranker() -> FlagReranker:
    global _reranker
    if _reranker is None:
        _reranker = FlagReranker(RERANKER_MODEL_PATH, use_fp16=True)
    return _reranker
