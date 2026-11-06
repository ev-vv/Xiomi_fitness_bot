"""Anthropic clients, prices, daily budget and user-facing error messages."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import anthropic

from core.config import settings

# USD per 1M tokens: (input, output, cache read). Cache writes cost 1.25x input.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}
WEB_SEARCH_USD = 0.01  # per search, on top of tokens
MODEL_NAMES = {"claude-haiku-5-5": "Claude Haiku 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
               "claude-opus-5-5": "Claude Opus 5.5"}


class BudgetExceeded(Exception):
    pass


class LLMRefused(Exception):
    pass


def _client_kwargs() -> dict[str, Any]:
    s = settings()
    # None lets the SDK fall back to its own env vars / defaults.
    return {
        "api_key": s.anthropic_api_key.get_secret_value() or None,
        "base_url": s.anthropic_base_url or None,
        "timeout": s.llm_timeout_s,
        "max_retries": 2,
    }


@lru_cache
def sync_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(**_client_kwargs())


@lru_cache
def async_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(**_client_kwargs())


def usage_dict(usage: Any) -> dict[str, int]:
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    u = {k: int(getattr(usage, k, 0) or 0) for k in keys}
    u["web_search_requests"] = int(getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0)
    return u


def cost(model: str, usage: dict[str, int]) -> float:
    inp, out, cread = PRICES.get(model, PRICES["claude-haiku-5-5"])
    tokens = (usage.get("input_tokens", 0) * inp + usage.get("output_tokens", 0) * out
              + usage.get("cache_read_input_tokens", 0) * cread
              + usage.get("cache_creation_input_tokens", 0) * inp * 1.25) / 1e6
    return tokens + usage.get("web_search_requests", 0) * WEB_SEARCH_USD


def record(route: str, model: str, usage: Any) -> float:
    from storage.db import add_usage

    u = usage if isinstance(usage, dict) else usage_dict(usage)
    c = cost(model, u)
    add_usage(route, model, u, c)
    return c


def check_budget() -> None:
    from storage.db import spent_today

    limit = settings().llm_daily_budget_usd
    if limit > 0 and spent_today() >= limit:
        raise BudgetExceeded(f"дневной лимит ${limit:.2f} исчерпан")


def describe_error(exc: BaseException) -> str:
    """Short Russian reason for a status line."""
    if isinstance(exc, TypeError) and "authentication" in str(exc):
        return "Не задан ключ ANTHROPIC_API_KEY"
    if isinstance(exc, BudgetExceeded):
        return f"Дневной лимит расходов на ИИ исчерпан ({exc})"
    if isinstance(exc, LLMRefused):
        return "Модель отказалась отвечать на этот запрос"
    if isinstance(exc, anthropic.AuthenticationError):
        return "Неверный или отсутствующий ключ ANTHROPIC_API_KEY"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "Доступ к Claude API запрещён (ключ или регион)"
    if isinstance(exc, anthropic.RateLimitError):
        return "Слишком много запросов к модели, попробуйте через минуту"
    if isinstance(exc, anthropic.APITimeoutError):
        return "Модель не ответила вовремя"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Нет связи с Claude API"
    if isinstance(exc, anthropic.APIStatusError):
        return f"Ошибка Claude API ({exc.status_code})" if exc.status_code < 500 else "Claude API временно недоступен"
    return f"{type(exc).__name__}: {exc}"


async def check_ready(model: str) -> tuple[bool, str]:
    """Free connectivity/auth check: model metadata, or token counting where a proxy lacks /v1/models/{id}."""
    client = async_client().with_options(timeout=20.0, max_retries=1)
    try:
        try:
            await client.models.retrieve(model)
        except anthropic.NotFoundError:
            await client.messages.count_tokens(model=model, messages=[{"role": "user", "content": "ping"}])
        return True, "Claude API доступен"
    except Exception as exc:  # noqa: BLE001 - every failure is a status for the user
        return False, describe_error(exc)
