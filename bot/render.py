"""Telegram HTML rendering of analyses, briefs, alerts and chat answers (pure functions)."""

from __future__ import annotations

import re
from html import escape
from typing import Any

LIMIT = 4096
CAPTION_LIMIT = 1024
_ALLOWED = re.compile(r"&lt;(/?)(b|i)&gt;")


def esc(s: Any) -> str:
    return escape(str(s), quote=False)


def caption(title: str, kpis: list[tuple[str, str]] | list[list[str]]) -> str:
    text = f"<b>{esc(title)}</b>"
    if kpis:
        text += "\n" + " · ".join(f"{esc(k)}: <b>{esc(v)}</b>" for k, v in kpis)
    return text[:CAPTION_LIMIT]


def _source_note(res: dict[str, Any]) -> str:
    if res.get("source") == "template":
        return f"\n\n<i>⚠️ Текст собран без ИИ: {esc(res.get('note') or 'модель недоступна')}.</i>"
    return ""


def analysis_html(res: dict[str, Any], period: str) -> tuple[str, str]:
    """(analysis block, advice block)."""
    out = res["out"]
    parts = [f"🧠 <b>Анализ</b>\n\n{esc(out['summary'])}"]
    if out.get("insights"):
        head = "🔎 <b>Инсайты</b>" if period != "day" else "🔎 <b>Детали</b>"
        parts.append(head + "\n" + "\n".join(f"• {esc(i['text'])}" for i in out["insights"]))
    if out.get("caveat"):
        parts.append(f"<i>ℹ️ {esc(out['caveat'])}</i>")
    analysis = "\n\n".join(parts) + _source_note(res)
    advice = "💡 <b>Советы</b>\n\n" + "\n".join(f"{n}. {esc(a['text'])}" for n, a in enumerate(out["advice"], 1))
    return analysis, advice


def brief_html(res: dict[str, Any], morning: bool = False) -> str:
    out = res["out"]
    head = "☀️ <b>Доброе утро! Сводка</b>" if morning else "📋 <b>Сводка дня</b>"
    lines = [head, f"<i>{esc(res.get('title', ''))}</i>", "", esc(out["summary"])]
    if res.get("facts"):
        lines += ["", "<blockquote expandable>" + "\n".join(f"• {esc(f)}" for f in res["facts"]) + "</blockquote>"]
    if out.get("advice"):
        lines += ["", "💡 " + "\n💡 ".join(esc(a["text"]) for a in out["advice"])]
    return "\n".join(lines) + _source_note(res)


def alert_html(a: dict[str, Any], day_label: str) -> str:
    from ai.facts import describe_anomaly

    titles = {"hr_spike": "❤️ Пульс выше нормы в покое", "bradycardia": "❤️ Низкий пульс днём",
              "sleep_hr_elevated": "😴 Повышенный пульс во сне"}
    return (f"⚠️ <b>{titles.get(a['type'], 'Необычное событие')}</b>\n{esc(day_label)}: {esc(describe_anomaly(a))}\n\n"
            "<i>Если чувствуете себя хорошо — скорее всего, это активность или волнение. "
            "Если повторяется или есть неприятные ощущения — обратитесь к врачу.</i>")


def chat_html(text: str) -> str:
    """Escapes model output, then restores the only allowed tags (<b>, <i>) if they are balanced."""
    s = _ALLOWED.sub(r"<\1\2>", esc(text))
    for tag in ("b", "i"):
        if s.count(f"<{tag}>") != s.count(f"</{tag}>"):
            s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
    return s


def sources_html(sources: list[tuple[str, str]]) -> str:
    """Web search citations as a link list under a chat answer."""
    if not sources:
        return ""
    links = (f'• <a href="{escape(url, quote=True)}">{esc(title[:80])}</a>' for title, url in sources)
    return "\n\n🌐 <b>Источники</b>\n" + "\n".join(links)


def plain(text: str) -> str:
    """Streaming preview: tags may be cut mid-way, so strip them."""
    return esc(re.sub(r"</?[bi]>", "", text))


def split(html: str, limit: int = LIMIT) -> list[str]:
    """Splits on paragraph/line boundaries so tags are never cut."""
    if len(html) <= limit:
        return [html]
    chunks, cur = [], ""
    for para in html.split("\n"):
        if len(cur) + len(para) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return chunks
