"""Streaming chat turn with tools.

History handling: past turns are kept as plain text (question, final answer). Tool calls and thinking
blocks live only inside the current turn and are sent back unchanged while it runs, so every request is
append-only within a turn and no earlier thinking block is ever edited.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from core.config import settings

from . import client as llm
from .prompts import CHAT_SYSTEM
from .tools import CHAT_TOOLS, label, run_tool

MAX_STEPS = 6
MAX_SOURCES = 5

OnStatus = Callable[[str], Awaitable[None]]
OnText = Callable[[str], Awaitable[None]]


class Cancelled(Exception):
    pass


@dataclass
class ChatResult:
    text: str
    tools: list[str] = field(default_factory=list)
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url) cited from web search
    calls: list[str] = field(default_factory=list)  # "name {args}" of every tool call, for the history note
    cost_usd: float = 0.0

    def history_text(self) -> str:
        """Answer as stored in history: the next turns must see which data and searches it was based on."""
        if not self.calls:
            return self.text
        note = "[Бот: для этого ответа выполнено: " + "; ".join(self.calls)
        if self.sources:
            note += ". Источники: " + "; ".join(url for _, url in self.sources)
        return f"{self.text}\n\n{note}]"


def _system(context: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": CHAT_SYSTEM, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": context},
    ]


def _call(name: str, args: Any) -> str:
    return f"{name} {json.dumps(args, ensure_ascii=False)}" if args else name


def _collect_sources(content: list[Any], sources: list[tuple[str, str]]) -> None:
    for block in content:
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and len(sources) < MAX_SOURCES and all(u != url for _, u in sources):
                sources.append((getattr(c, "title", None) or url, url))


async def answer(question: str, history: list[dict[str, Any]], context: str, *, on_status: OnStatus,
                 on_text: OnText, cancel: asyncio.Event | None = None,
                 client: anthropic.AsyncAnthropic | None = None) -> ChatResult:
    cfg = settings()
    llm.check_budget()
    client = client or llm.async_client()
    model = cfg.llm_model_chat
    messages: list[dict[str, Any]] = [*history, {"role": "user", "content": question}]
    result = ChatResult(text="")
    for _ in range(MAX_STEPS):
        async with client.messages.stream(
            model=model, max_tokens=4000, system=_system(context), tools=CHAT_TOOLS, messages=messages,
            output_config={"effort": cfg.llm_effort_chat},
        ) as stream:
            async for event in stream:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if event.type == "content_block_start" and event.content_block.type in ("tool_use", "server_tool_use"):
                    await on_status(label(event.content_block.name, {}))
                elif event.type == "text":
                    result.text += event.text
                    await on_text(event.text)
            final = await stream.get_final_message()
        result.cost_usd += llm.record("chat", model, final.usage)
        if final.stop_reason == "refusal":
            raise llm.LLMRefused()
        _collect_sources(final.content, result.sources)
        for b in final.content:
            if b.type == "server_tool_use":
                result.tools.append(b.name)
                result.calls.append(_call(b.name, b.input))
        if final.stop_reason == "pause_turn":  # server-side search loop paused: resend to let it resume
            messages.append({"role": "assistant", "content": [b.to_dict() for b in final.content]})
            continue
        tool_uses = [b for b in final.content if b.type == "tool_use"]
        if final.stop_reason != "tool_use" or not tool_uses:
            break
        messages.append({"role": "assistant", "content": [b.to_dict() for b in final.content]})
        results = []
        for block in tool_uses:
            args = block.input if isinstance(block.input, dict) else {}
            await on_status(label(block.name, args))
            text, is_error = await asyncio.to_thread(run_tool, block.name, args)
            result.tools.append(block.name)
            result.calls.append(_call(block.name, args))
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": text, "is_error": is_error})
        messages.append({"role": "user", "content": results})
        if result.text and not result.text.endswith("\n"):
            result.text += "\n\n"
            await on_text("\n\n")
    return result
