"""Web search integration and routing helpers."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from config import (
    ENABLE_WEB_SEARCH,
    SERPER_API_KEY,
    WEB_SEARCH_MAX_CONTEXT_CHARS,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MIN_CONFIDENCE,
    WEB_SEARCH_SAFE,
    WEB_SEARCH_TIMEOUT_SEC,
)

_REALTIME_HINTS = (
    "today",
    "latest",
    "recent",
    "news",
    "current",
    "now",
    "update",
    "deadline",
    "weather",
    "stock",
    "price",
    "今天",
    "最新",
    "最近",
    "新闻",
    "现在",
    "实时",
    "更新",
    "截止",
    "天气",
    "汇率",
)

_COMMAND_HINTS = (
    "search",
    "web",
    "google",
    "查一下",
    "搜索",
    "上网查",
)


@dataclass
class WebSearchItem:
    title: str
    url: str
    snippet: str
    source: str
    confidence: float


def _domain_from_url(url: str) -> str:
    no_proto = url.split("//", 1)[-1]
    return no_proto.split("/", 1)[0].lower()


def should_trigger_web_search(user_text: str, kb_stats: dict | None = None) -> tuple[bool, str]:
    if not ENABLE_WEB_SEARCH:
        return False, "disabled"

    query = user_text.lower().strip()
    if not query:
        return False, "empty_query"

    if any(h in query for h in _COMMAND_HINTS):
        return True, "explicit_search_hint"

    if any(h in query for h in _REALTIME_HINTS):
        return True, "realtime_hint"

    if kb_stats:
        sections = int(kb_stats.get("sections_used", 0) or 0)
        context_chars = int(kb_stats.get("context_chars", 0) or 0)
        if sections == 0 or context_chars < 200:
            return True, "kb_low_coverage"

    return False, "not_needed"


def _normalize_items(payload: dict) -> list[WebSearchItem]:
    items: list[WebSearchItem] = []
    organic = payload.get("organic", [])
    for row in organic:
        title = str(row.get("title", "")).strip()
        url = str(row.get("link", "")).strip()
        snippet = str(row.get("snippet", "")).strip()
        if not title or not url:
            continue
        source = _domain_from_url(url)
        confidence = 0.7
        if source.endswith(".edu") or source.endswith(".edu.cn"):
            confidence = 0.9
        elif source.endswith(".gov") or source.endswith(".gov.cn"):
            confidence = 0.85
        items.append(
            WebSearchItem(
                title=title,
                url=url,
                snippet=snippet,
                source=source,
                confidence=confidence,
            )
        )

    deduped: list[WebSearchItem] = []
    seen: set[str] = set()
    for item in items:
        key = re.sub(r"/$", "", item.url)
        if key in seen:
            continue
        seen.add(key)
        if item.confidence >= WEB_SEARCH_MIN_CONFIDENCE:
            deduped.append(item)

    return deduped[:WEB_SEARCH_MAX_RESULTS]


def search_web_serper(query: str) -> tuple[list[WebSearchItem], str | None]:
    if not SERPER_API_KEY:
        return [], "missing_api_key"

    body = {
        "q": query,
        "num": WEB_SEARCH_MAX_RESULTS,
        "gl": "cn",
        "hl": "zh-cn",
        "safe": WEB_SEARCH_SAFE,
    }
    req = urllib.request.Request(
        url="https://google.serper.dev/search",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "X-API-KEY": SERPER_API_KEY,
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=WEB_SEARCH_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"http_{exc.code}"
    except urllib.error.URLError:
        return [], "network_error"
    except TimeoutError:
        return [], "timeout"
    except Exception:
        return [], "unknown_error"

    return _normalize_items(payload), None


def maybe_web_search(query: str, kb_stats: dict | None = None) -> tuple[list[WebSearchItem], str]:
    should_search, reason = should_trigger_web_search(query, kb_stats)
    if not should_search:
        return [], reason

    results, err = search_web_serper(query)
    if err:
        return [], f"search_failed:{err}"
    if not results:
        return [], "search_empty"
    return results, reason


def build_web_context(results: list[WebSearchItem]) -> str:
    if not results:
        return ""

    chunks: list[str] = []
    budget = WEB_SEARCH_MAX_CONTEXT_CHARS
    for idx, item in enumerate(results, start=1):
        line = (
            f"[{idx}] {item.title}\n"
            f"URL: {item.url}\n"
            f"Source: {item.source}\n"
            f"Snippet: {item.snippet}\n"
        )
        if len(line) > budget:
            break
        chunks.append(line)
        budget -= len(line)

    return "\n".join(chunks).strip()


def build_sources_list(results: list[WebSearchItem], max_items: int) -> list[str]:
    sources: list[str] = []
    seen: set[str] = set()
    for item in results:
        if item.url in seen:
            continue
        seen.add(item.url)
        sources.append(f"{item.title} - {item.url}")
        if len(sources) >= max_items:
            break
    return sources
