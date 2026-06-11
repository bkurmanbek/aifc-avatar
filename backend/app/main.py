from __future__ import annotations

import os

from backend.core.logging import configure_logging

configure_logging(reset=os.getenv("RESET_LOGS_ON_START", "true").lower() not in {"0", "false", "no", "off"})

from backend.legacy.app import app  # noqa: E402

__all__ = ["app"]
