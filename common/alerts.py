"""Operator alerting: structured CRITICAL/WARN logging plus an optional
webhook POST (Telegram-bot compatible payload shape: {"text": ...}).

The webhook is fire-and-forget with a short timeout — an alerting outage must
never block or crash trading code.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger("alerts")

_LEVELS = {"INFO": logging.INFO, "WARN": logging.WARNING, "CRITICAL": logging.CRITICAL}


def alert(level: str, msg: str, **ctx: Any) -> None:
    log_level = _LEVELS.get(level.upper(), logging.WARNING)
    logger.log(log_level, "%s | %s", msg, json.dumps(ctx, default=str) if ctx else "")

    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        text = f"[{level.upper()}] {msg}"
        if ctx:
            text += "\n" + json.dumps(ctx, default=str, indent=2)
        requests.post(url, json={"text": text}, timeout=5)
    except Exception as exc:  # alerting must never take the trader down
        logger.warning("alert webhook failed: %s", exc)
