"""Local deterministic tools that should run before KB/Web retrieval."""

from __future__ import annotations

import datetime as dt
import re

_DATE_PATTERNS = (
    r"\bwhat date is (it|today)\b",
    r"\bwhat'?s the date\b",
    r"\bwhat day is (it|today)\b",
    r"\btoday'?s date\b",
    r"\b今天几号\b",
    r"\b今天几月几号\b",
    r"\b今天星期几\b",
)

_TIME_PATTERNS = (
    r"\bwhat time is it\b",
    r"\bcurrent time\b",
    r"\b现在几点\b",
)


def _is_match(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def maybe_answer_local(query: str) -> tuple[str | None, str]:
    text = query.strip()
    if not text:
        return None, "empty_query"

    now = dt.datetime.now()
    if _is_match(text, _DATE_PATTERNS):
        weekday = now.strftime("%A")
        return f"Today is {now:%Y-%m-%d} ({weekday}).", "local_deterministic_date"

    if _is_match(text, _TIME_PATTERNS):
        return f"Current local time is {now:%H:%M:%S}.", "local_deterministic_time"

    return None, "not_local_tool"
