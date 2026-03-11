"""Knowledge-base loader with hybrid TF-IDF + embedding retrieval.

Supports two modes:
  1. **Directory mode** (preferred): loads every .txt file under
     ``KNOWLEDGE_DIR``.  Files are discovered recursively so you can
     organise topics into sub-folders.
  2. **Single-file fallback**: loads ``KNOWLEDGE_BASE_PATH`` if the
     directory does not exist.

Each file is split by ``[Section Header]`` markers.  At query time
only the most relevant sections are injected into the LLM prompt.

Retrieval (v3 — hybrid):
  * **Embedding score** — cosine similarity via sentence-transformers
    (``all-MiniLM-L6-v2``).  Captures semantic meaning.
  * **TF-IDF score** — keyword overlap with IDF weighting.
    Catches exact keyword matches that embeddings may miss.
  * **Hybrid** — weighted combination: ``EMBEDDING_WEIGHT * emb +
    TFIDF_WEIGHT * tfidf``.  Falls back to TF-IDF only if
    sentence-transformers is unavailable.

Optimizations:
  * **Auto-chunking** — sections larger than ``KB_MAX_SECTION_CHARS``
    are split into sub-sections of roughly ``KB_CHUNK_TARGET_CHARS``.
  * **Score threshold** — sections below ``KB_MIN_RELEVANCE_SCORE``
    are dropped even if they fall in the top-K.
  * **Relative threshold** — sections scoring < ``KB_RELATIVE_THRESHOLD``
    of the top hit are dropped.
  * **Context budget** — total context capped at ``KB_MAX_CONTEXT_CHARS``.
  * **Section truncation** — individual sections capped at
    ``KB_MAX_INJECT_CHARS``.
"""

import os
import re
import math
import glob
import numpy as np
from collections import Counter
from typing import List, Tuple, Optional

from config import (
    KNOWLEDGE_BASE_PATH,
    KNOWLEDGE_DIR,
    KB_TOP_K_SECTIONS,
    KB_MAX_CONTEXT_CHARS,
    KB_MIN_RELEVANCE_SCORE,
    KB_RELATIVE_THRESHOLD,
    KB_MAX_SECTION_CHARS,
    KB_CHUNK_TARGET_CHARS,
    KB_MAX_INJECT_CHARS,
    EMBEDDING_MODEL_NAME,
    EMBEDDING_WEIGHT,
    TFIDF_WEIGHT,
)


# ═══════════════════════════════════════════════════════════
#  Section data structure
# ═══════════════════════════════════════════════════════════
class KBSection:
    """One [Section] block, possibly from a specific source file."""

    __slots__ = ("title", "body", "source", "_tokens", "_token_counts", "_norm")

    def __init__(self, title: str, body: str, source: str = ""):
        self.title = title
        self.body = body
        self.source = source
        self._tokens = _tokenize(f"{title} {body}")
        self._token_counts = Counter(self._tokens)
        self._norm = math.sqrt(sum(v * v for v in self._token_counts.values())) or 1.0

    def full_text(self, max_chars: int = 0) -> str:
        """Return '[Title]\nbody', optionally truncated to *max_chars*."""
        header = f"[{self.title}]"
        if max_chars > 0 and len(self.body) > max_chars:
            return f"{header}\n{self.body[:max_chars]}..."
        return f"{header}\n{self.body}"

    def __repr__(self):
        tag = f" from {self.source}" if self.source else ""
        return f"<KBSection: {self.title} ({len(self.body)} chars){tag}>"


# ═══════════════════════════════════════════════════════════
#  Tokeniser
# ═══════════════════════════════════════════════════════════
_SPLIT_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")

_STOPWORDS = frozenset({
    "the", "is", "at", "in", "on", "to", "of", "and", "or", "for",
    "an", "be", "by", "it", "if", "as", "no", "do", "so", "up",
    "are", "was", "not", "but", "you", "your", "all", "can", "has",
    "may", "will", "with", "this", "that", "from", "have", "they",
    "been", "its", "also", "than", "each", "any", "our", "their",
    "about", "when", "which", "there", "these", "those", "would",
    "should", "could", "what", "where", "how", "who", "whom",
    "please", "want", "need", "know", "tell", "me", "my", "get",
})


def _tokenize(text: str) -> List[str]:
    """Lowercase -> split on non-alphanum -> drop stopwords & 1-char tokens."""
    return [t for t in _SPLIT_RE.split(text.lower())
            if len(t) > 1 and t not in _STOPWORDS]


# ═══════════════════════════════════════════════════════════
#  Parser
# ═══════════════════════════════════════════════════════════
_HEADER_RE = re.compile(r"^\[(.+?)\]\s*$", re.MULTILINE)


def _parse_sections(raw: str, source: str = "") -> List[KBSection]:
    """Split raw text into KBSection objects by [Header] markers."""
    matches = list(_HEADER_RE.finditer(raw))
    if not matches:
        stripped = raw.strip()
        if stripped:
            return [KBSection("General", stripped, source)]
        return []

    sections: List[KBSection] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end].strip()
        if body:
            sections.append(KBSection(title, body, source))
    return sections


# ═══════════════════════════════════════════════════════════
#  Auto-chunking
# ═══════════════════════════════════════════════════════════
_PARAGRAPH_RE = re.compile(r"\n{2,}")


def _chunk_section(
    section: KBSection,
    max_chars: int = KB_MAX_SECTION_CHARS,
    target: int = KB_CHUNK_TARGET_CHARS,
) -> List[KBSection]:
    """
    If *section* body exceeds *max_chars*, split it into sub-sections
    at paragraph boundaries, each roughly *target* chars.
    Short sections are returned unchanged (as a list of one).
    """
    if len(section.body) <= max_chars:
        return [section]

    paragraphs = _PARAGRAPH_RE.split(section.body)
    chunks: List[KBSection] = []
    current_lines: List[str] = []
    current_len = 0
    part = 1

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        # If adding this paragraph exceeds target AND we already have
        # content, start a new chunk.
        if current_len + len(para) > target and current_lines:
            body = "\n\n".join(current_lines)
            chunks.append(KBSection(
                f"{section.title} (Part {part})",
                body,
                section.source,
            ))
            part += 1
            current_lines = []
            current_len = 0
        current_lines.append(para)
        current_len += len(para)

    if current_lines:
        body = "\n\n".join(current_lines)
        chunks.append(KBSection(
            f"{section.title} (Part {part})" if part > 1 else section.title,
            body,
            section.source,
        ))

    return chunks


# ═══════════════════════════════════════════════════════════
#  Scoring
# ═══════════════════════════════════════════════════════════
def _score(query_tokens: List[str], section: KBSection, idf: dict) -> float:
    """TF-IDF cosine similarity."""
    query_counts = Counter(query_tokens)

    dot = 0.0
    for t in query_counts:
        if t in section._token_counts:
            w = idf.get(t, 1.0)
            dot += query_counts[t] * section._token_counts[t] * w * w

    q_norm = math.sqrt(sum((v * idf.get(t, 1.0)) ** 2
                           for t, v in query_counts.items())) or 1.0
    s_norm = math.sqrt(sum((v * idf.get(t, 1.0)) ** 2
                           for t, v in section._token_counts.items())) or 1.0
    return dot / (q_norm * s_norm)


# ═══════════════════════════════════════════════════════════
#  Embedding Engine (optional — graceful degradation)
# ═══════════════════════════════════════════════════════════
class EmbeddingEngine:
    """Lazy-loaded sentence-transformer for semantic similarity."""

    def __init__(self, model_name: str = EMBEDDING_MODEL_NAME):
        self._model_name = model_name
        self._model = None
        self._available: Optional[bool] = None
        self._section_embeddings: Optional[np.ndarray] = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._available = True
            except ImportError:
                self._available = False
                print("[KB]: sentence-transformers not installed — "
                      "using TF-IDF only.")
        return self._available

    def _ensure_model(self):
        if self._model is None and self.available:
            from sentence_transformers import SentenceTransformer
            print(f"[KB]: Loading embedding model '{self._model_name}'...")
            self._model = SentenceTransformer(self._model_name)
            print(f"[KB]: Embedding model ready.")

    def index(self, sections: List[KBSection]):
        """Pre-compute embeddings for all sections."""
        if not self.available:
            return
        self._ensure_model()
        texts = [f"{s.title}: {s.body}" for s in sections]
        self._section_embeddings = self._model.encode(
            texts, normalize_embeddings=True, show_progress_bar=False
        )

    def query_scores(self, query: str, n_sections: int) -> Optional[np.ndarray]:
        """Return cosine similarity scores for query vs all sections.

        Returns an array of shape (n_sections,), or None if unavailable.
        """
        if not self.available or self._section_embeddings is None:
            return None
        if self._section_embeddings.shape[0] != n_sections:
            return None
        self._ensure_model()
        q_emb = self._model.encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        )
        # Cosine similarity (embeddings are already normalized)
        scores = (self._section_embeddings @ q_emb.T).flatten()
        return scores


# ═══════════════════════════════════════════════════════════
#  KnowledgeBase
# ═══════════════════════════════════════════════════════════
class KnowledgeBase:
    """
    Load, index, and retrieve campus knowledge.

    Uses hybrid scoring: embedding similarity + TF-IDF.
    Falls back to TF-IDF only if sentence-transformers is unavailable.

    Priority:
      1. If ``KNOWLEDGE_DIR`` exists -> load all .txt files within it.
      2. Else fall back to single ``KNOWLEDGE_BASE_PATH``.

    Usage::

        kb = KnowledgeBase()
        context = kb.retrieve("Where is the library?")
    """

    def __init__(
        self,
        directory: str = KNOWLEDGE_DIR,
        fallback_path: str = KNOWLEDGE_BASE_PATH,
    ):
        self.directory = directory
        self.fallback_path = fallback_path
        self.sections: List[KBSection] = []
        self.file_count: int = 0
        self._idf: dict = {}
        self._embedder = EmbeddingEngine()
        self.load()

    def _compute_idf(self):
        n = len(self.sections)
        if n == 0:
            return
        df: Counter = Counter()
        for s in self.sections:
            for t in set(s._tokens):
                df[t] += 1
        self._idf = {t: math.log(n / freq) + 1.0 for t, freq in df.items()}

    def load(self):
        """(Re)load all knowledge files."""
        self.sections.clear()
        self.file_count = 0

        raw_sections: List[KBSection] = []

        if os.path.isdir(self.directory):
            self._load_directory(self.directory, raw_sections)
        elif os.path.isfile(self.fallback_path):
            self._load_file(self.fallback_path, raw_sections)
        else:
            print(f"[KB Warning]: No knowledge source found.")
            print(f"  Tried dir:  {self.directory}")
            print(f"  Tried file: {self.fallback_path}")
            self.sections = [
                KBSection("Notice",
                          "No local knowledge base found. "
                          "Please contact administrator.")
            ]
            return

        # Auto-chunk large sections
        chunked_count = 0
        for sec in raw_sections:
            chunks = _chunk_section(sec)
            if len(chunks) > 1:
                chunked_count += 1
            self.sections.extend(chunks)

        self._compute_idf()

        # Build embedding index
        self._embedder.index(self.sections)

        msg = (f"[KB]: Loaded {len(self.sections)} sections "
               f"from {self.file_count} file(s)")
        if chunked_count:
            msg += f" ({chunked_count} large sections auto-chunked)"
        if self._embedder.available:
            msg += " [hybrid: embedding + TF-IDF]"
        else:
            msg += " [TF-IDF only]"
        print(msg)

    def _load_directory(self, dirpath: str, out: List[KBSection]):
        pattern = os.path.join(dirpath, "**", "*.txt")
        files = sorted(glob.glob(pattern, recursive=True))
        if not files:
            print(f"[KB Warning]: Directory {dirpath} contains no .txt files")
            return
        for fpath in files:
            self._load_file(fpath, out)

    def _load_file(self, fpath: str, out: List[KBSection]):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if not raw:
                return
            source = os.path.basename(fpath)
            new_sections = _parse_sections(raw, source=source)
            out.extend(new_sections)
            self.file_count += 1
        except Exception as e:
            print(f"[KB Error]: Failed to load {fpath}: {e}")

    def _hybrid_scores(self, query: str) -> List[Tuple[float, int]]:
        """Compute hybrid (embedding + TF-IDF) scores for all sections.

        Returns a list of (hybrid_score, section_index), sorted descending.
        If embeddings are unavailable, falls back to TF-IDF only.
        """
        n = len(self.sections)
        query_tokens = _tokenize(query)

        # TF-IDF scores
        tfidf_raw = np.array(
            [_score(query_tokens, s, self._idf) for s in self.sections],
            dtype=np.float32,
        )

        # Embedding scores
        emb_raw = self._embedder.query_scores(query, n)

        if emb_raw is not None:
            # Normalize each to [0, 1] range for fair combination
            tfidf_max = tfidf_raw.max() or 1.0
            emb_max = emb_raw.max() or 1.0
            tfidf_norm = tfidf_raw / tfidf_max
            emb_norm = emb_raw / emb_max
            hybrid = EMBEDDING_WEIGHT * emb_norm + TFIDF_WEIGHT * tfidf_norm
        else:
            hybrid = tfidf_raw

        ranked = sorted(enumerate(hybrid), key=lambda x: x[1], reverse=True)
        return [(float(score), idx) for idx, score in ranked]

    def retrieve(
        self,
        query: str,
        top_k: int = KB_TOP_K_SECTIONS,
        max_context_chars: int = KB_MAX_CONTEXT_CHARS,
        min_score: float = KB_MIN_RELEVANCE_SCORE,
        rel_threshold: float = KB_RELATIVE_THRESHOLD,
        max_inject: int = KB_MAX_INJECT_CHARS,
    ) -> str:
        """
        Return the most relevant sections as context text.

        Uses hybrid scoring (embedding + TF-IDF) when available.

        Applies four filters in order:
          1. Absolute score threshold — drop below *min_score*.
          2. Relative threshold — drop below *rel_threshold* × top score.
          3. Top-K — keep at most *top_k* sections.
          4. Context budget — greedily fill up to *max_context_chars*.

        Each section is truncated to *max_inject* chars.
        """
        if not self.sections:
            return "[No knowledge base available.]"

        query_tokens = _tokenize(query)
        if not query_tokens:
            result, budget = [], max_context_chars
            for s in self.sections[:top_k]:
                txt = s.full_text(max_inject)
                if budget - len(txt) < 0:
                    break
                result.append(txt)
                budget -= len(txt)
            return "\n\n".join(result) if result else self.sections[0].full_text(max_inject)

        ranked = self._hybrid_scores(query)

        top_score = ranked[0][0] if ranked else 0.0
        rel_cutoff = top_score * rel_threshold

        result: List[str] = []
        budget = max_context_chars
        count = 0
        for sc, idx in ranked:
            if count >= top_k:
                break
            if sc < min_score:
                break
            if sc < rel_cutoff and result:
                break
            sec = self.sections[idx]
            txt = sec.full_text(max_inject)
            cost = len(txt)
            if budget - cost < 0 and result:
                break
            result.append(txt)
            budget -= cost
            count += 1

        return "\n\n".join(result)

    def retrieve_debug(
        self,
        query: str,
        top_k: int = KB_TOP_K_SECTIONS,
        max_context_chars: int = KB_MAX_CONTEXT_CHARS,
        min_score: float = KB_MIN_RELEVANCE_SCORE,
        rel_threshold: float = KB_RELATIVE_THRESHOLD,
        max_inject: int = KB_MAX_INJECT_CHARS,
    ) -> Tuple[str, dict]:
        """
        Like ``retrieve`` but also returns a stats dict with
        token budget info for diagnostics.
        """
        if not self.sections:
            return "[No knowledge base available.]", {"sections_used": 0}

        query_tokens = _tokenize(query)
        if not query_tokens:
            ctx = self.retrieve(query)
            return ctx, {"sections_used": 0, "note": "no query tokens"}

        ranked = self._hybrid_scores(query)

        top_score = ranked[0][0] if ranked else 0.0
        rel_cutoff = top_score * rel_threshold

        result: List[str] = []
        budget = max_context_chars
        count = 0
        used_sections = []
        for sc, idx in ranked:
            if count >= top_k:
                break
            if sc < min_score:
                break
            if sc < rel_cutoff and result:
                break
            sec = self.sections[idx]
            txt = sec.full_text(max_inject)
            cost = len(txt)
            if budget - cost < 0 and result:
                break
            result.append(txt)
            budget -= cost
            count += 1
            used_sections.append((sc, sec.title, sec.source, cost))

        ctx = "\n\n".join(result)
        stats = {
            "sections_used": count,
            "context_chars": len(ctx),
            "context_tokens_est": len(ctx) // 4,
            "budget_remaining": budget,
            "details": used_sections,
        }
        return ctx, stats

    def get_all(self) -> str:
        return "\n\n".join(s.full_text() for s in self.sections)

    def search_debug(self, query: str, top_k: int = 10):
        """Print scored ranking for debugging retrieval."""
        ranked = self._hybrid_scores(query)
        print(f"Query: '{query}'")
        print(f"Mode: {'hybrid (emb+tfidf)' if self._embedder.available else 'TF-IDF only'}")
        for rank, (sc, idx) in enumerate(ranked[:top_k], 1):
            sec = self.sections[idx]
            print(f"  #{rank}  score={sc:.4f}  [{sec.title}]  ({sec.source})  "
                  f"{len(sec.body)} chars")

    def __repr__(self):
        return (f"<KnowledgeBase: {len(self.sections)} sections "
                f"from {self.file_count} file(s)>")
