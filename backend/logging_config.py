from __future__ import annotations

import logging
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"

LOG_FILES = {
    "backend": "backend.log",
    "stt": "stt.log",
    "llm": "llm.log",
    "tts": "tts.log",
    "avatar": "avatar.log",
    "pipeline": "pipeline.log",
    "websocket": "websocket.log",
    "errors": "errors.log",
}

_CONFIGURED = False


class PrefixFilter(logging.Filter):
    def __init__(self, prefixes: tuple[str, ...]):
        super().__init__()
        self.prefixes = prefixes

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith(self.prefixes)


def reset_logs() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    for path in LOG_DIR.iterdir():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    for filename in LOG_FILES.values():
        (LOG_DIR / filename).touch()


def configure_logging(*, reset: bool = False) -> None:
    global _CONFIGURED
    if reset:
        reset_logs()
    else:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    if _CONFIGURED:
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    backend_handler = _file_handler(LOG_FILES["backend"], formatter)
    root.addHandler(backend_handler)

    error_handler = _file_handler(LOG_FILES["errors"], formatter)
    error_handler.setLevel(logging.ERROR)
    root.addHandler(error_handler)

    module_filters: dict[str, tuple[str, ...]] = {
        "stt": ("backend.stt",),
        "llm": ("backend.llm", "backend.answer_race"),
        "tts": ("backend.soniox_tts",),
        "avatar": ("backend.synctalk",),
        "pipeline": ("backend.main", "backend.response_stream", "backend.answer_format", "backend.intro", "backend.startup"),
        "websocket": ("backend.ws_writer", "backend.client"),
    }
    for module, prefixes in module_filters.items():
        handler = _file_handler(LOG_FILES[module], formatter)
        handler.addFilter(PrefixFilter(prefixes))
        root.addHandler(handler)

    _CONFIGURED = True


def _file_handler(filename: str, formatter: logging.Formatter) -> logging.FileHandler:
    path = LOG_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    return handler


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    session_id: str | None = None,
    request_id: str | None = None,
    latency_ms: int | float | None = None,
    level: int = logging.INFO,
    error: BaseException | None = None,
    **fields: Any,
) -> None:
    parts = [f"event={event}"]
    if session_id:
        parts.append(f"session_id={session_id}")
    if request_id:
        parts.append(f"request_id={request_id}")
    if latency_ms is not None:
        parts.append(f"latency_ms={int(latency_ms)}")
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", "\\n")
        parts.append(f"{key}={text}")
    if error is not None:
        parts.append(f"error={type(error).__name__}: {error}")
        parts.append("stack=" + "".join(traceback.format_exception(error)).replace("\n", "\\n"))
    logger.log(level, " ".join(parts), exc_info=error if error else None)
