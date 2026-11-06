"""Minimal Telegram Bot API sender for the worker (morning brief, alerts) — no aiogram needed there."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


def _post(method: str, data: dict[str, Any], files: dict[str, Any] | None = None) -> bool:
    cfg = settings()
    url = f"https://api.telegram.org/bot{cfg.bot_token.get_secret_value()}/{method}"
    try:
        with httpx.Client(proxy=cfg.telegram_proxy or None, timeout=30) as http:
            resp = http.post(url, data=data, files=files) if files else http.post(url, json=data)
        if resp.status_code != 200:
            log.warning("telegram %s failed: %s %s", method, resp.status_code, resp.text[:200])
        return resp.status_code == 200
    except httpx.HTTPError as exc:
        log.warning("telegram %s failed: %s", method, exc)
        return False


def send_message(chat_id: int, html: str, reply_markup: dict[str, Any] | None = None) -> bool:
    data: dict[str, Any] = {"chat_id": chat_id, "text": html, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        data["reply_markup"] = reply_markup
    return _post("sendMessage", data)

