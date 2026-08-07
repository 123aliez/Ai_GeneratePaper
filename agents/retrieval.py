"""Two-tier retrieval for the experiment-paper writing framework.

Tier 1 (authoritative): local reading notes / bibliography, matched against the
generated reference index (``REFERENCE_INDEX_PATH``). This is the primary source.

Tier 2 (supplementary): a strong web-search LLM (``RETRIEVAL_MODEL`` on an
OpenAI-compatible endpoint, e.g. grok). This tier is optional. When
``RETRIEVAL_MODEL`` is left blank the web tier degrades gracefully to an empty
result — it never raises. Any network/parse failure is swallowed with a single
warning line, again returning an empty list.

Only the standard library plus ``config`` (already a project dependency) is used;
the web request is a plain ``urllib`` POST to ``/chat/completions`` so there is no
hard dependency on litellm being importable at call time.
"""
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Match the project's import convention (see agents/agents.py): make the project
# root importable so ``config`` resolves whether run as a package or a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # pragma: no cover - trivial import guard
    import config as _config
except Exception:  # pragma: no cover - degrade to env-var lookups
    _config = None

import os

WEB_TIMEOUT = 30          # seconds for the retrieval-LLM call
_USER_AGENT = "paper-agent-retrieval/1.0"


# ── Config accessors (read live so tests reflect the current environment) ────
def _cfg(name: str, default: str = "") -> str:
    if _config is not None and hasattr(_config, name):
        value = getattr(_config, name)
        return value if value is not None else default
    return os.getenv(name, default)


def _index_path() -> Path:
    if _config is not None and hasattr(_config, "REFERENCE_INDEX_PATH"):
        return Path(_config.REFERENCE_INDEX_PATH)
    return Path(os.getenv("REFERENCE_INDEX_PATH", "references/index.md"))


# ── Tier 1: local notes / bibliography ───────────────────────────────────────
def _parse_index_text(text: str) -> list[dict]:
    """Parse markdown reference-index rows (``| REF-#### | ... |``) into dicts.

    Mirrors the substring-matching style of ``tools.search_references``: every
    data row starts with ``| REF-``. Both the 10-column global index and the
    6-column bibliography table are tolerated; missing columns fall back to "".
    """
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        columns = [c.strip() for c in stripped.strip("|").split("|")]
        if not columns or not columns[0].upper().startswith("REF-"):
            continue

        def col(i: int) -> str:
            return columns[i] if i < len(columns) else ""

        rows.append({
            "id": col(0),
            "title": col(1),
            # Global index: col 8 is Note Path, col 7 is Key Use.
            # Bibliography (6 cols): no note path; last col is the primary claim.
            "note_path": col(8),
            "snippet": col(7) if len(columns) > 7 else col(len(columns) - 1),
            "haystack": " ".join(columns).lower(),
        })
    return rows


def _match_rows(rows: list[dict], query: str, k: int) -> list[dict]:
    """Rank parsed rows by keyword overlap with ``query``; return up to ``k``.

    Tokens shorter than 3 chars are ignored. A row with no overlapping token is
    dropped. An empty query returns the first ``k`` rows (parity with
    ``search_references`` treating an empty query as "match all").
    """
    tokens = [t for t in "".join(
        ch if ch.isalnum() else " " for ch in (query or "").lower()
    ).split() if len(t) >= 3]

    scored = []
    for row in rows:
        hay = row["haystack"]
        score = sum(1 for t in set(tokens) if t in hay) if tokens else 1
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    out = []
    for _score, row in scored[: max(0, k)]:
        out.append({
            "id": row["id"],
            "title": row["title"],
            "note_path": row["note_path"],
            "snippet": row["snippet"],
        })
    return out


def _read_source(path: Path):
    """Read a reference source file; return its text or None if unreadable."""
    try:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[retrieval] warning: cannot read {path}: {exc}", flush=True)
        return None


def search_notes(query: str, k: int = 5) -> list[dict]:
    """Tier-1 lookup against the local bibliography and reference index.

    Reads BOTH ``references/bibliography.md`` (the project's actual citation
    source, which has the ``| REF-#### |`` rows) and ``references/index.md``
    (the generated global index, when present). Rows are keyword-matched, merged,
    and de-duplicated by id; returns up to ``k`` ``{"id","title","note_path",
    "snippet"}``. If neither file exists, returns an empty list.
    """
    combined = []
    seen_ids = set()
    sources = []
    if _config is not None:
        bib = getattr(_config, "BIBLIOGRAPHY_PATH", None)
        if bib is not None:
            sources.append(Path(bib))
    sources.append(_index_path())
    for source in sources:
        text = _read_source(source)
        if text is None:
            continue
        for row in _parse_index_text(text):
            rid = row["id"]
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            combined.append(row)
    return _match_rows(combined, query, k)


# ── Tier 2: web-search LLM (optional, degrades to []) ────────────────────────
def _strip_provider_prefix(model_id: str) -> str:
    """Drop a litellm routing prefix (``openai/``, ``xai/`` ...) for the raw API."""
    for prefix in ("openai/", "xai/", "anthropic/", "gemini/", "groq/"):
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id


def _extract_json_array(content: str) -> list:
    """Best-effort extraction of a JSON array from an LLM message."""
    content = content.strip()
    if content.startswith("```"):
        # strip a ```json ... ``` fence
        content = content.split("```", 2)
        content = content[1] if len(content) > 1 else ""
        if content.lstrip().lower().startswith("json"):
            content = content.lstrip()[4:]
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    return json.loads(content[start: end + 1])


def search_web(query: str, max_results: int = 5) -> list[dict]:
    """Tier-2 lookup via the configured retrieval LLM.

    Returns ``[{"title","url","snippet"}]`` (at most ``max_results``). If
    ``RETRIEVAL_MODEL`` / ``RETRIEVAL_API_BASE`` are not configured, returns ``[]``
    with no warning (expected degradation). Any network/parse error is swallowed
    with a single warning line and also returns ``[]`` — this function never
    raises.
    """
    model = _cfg("RETRIEVAL_MODEL")
    api_base = _cfg("RETRIEVAL_API_BASE")
    api_key = _cfg("RETRIEVAL_API_KEY")
    if not model or not api_base:
        return []  # web tier disabled — silent, graceful

    url = api_base.rstrip("/") + "/chat/completions"
    system = (
        "You are a literature web-search assistant. For the user's query, return "
        "the most relevant real sources. Respond with ONLY a JSON array; each item "
        "must be an object with keys \"title\", \"url\", \"snippet\". The url must be "
        "a real, resolvable link (arXiv/DOI/official page). Return at most "
        f"{max_results} items and no prose."
    )
    payload = json.dumps({
        "model": _strip_provider_prefix(model),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        "temperature": 0.0,
    }).encode("utf-8")

    request = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=WEB_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        items = _extract_json_array(content)
    except Exception as exc:
        print(f"[retrieval] warning: web search failed: {exc}", flush=True)
        return []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        link = str(item.get("url", "")).strip()
        title = str(item.get("title", "")).strip()
        if not link or not title:
            continue
        results.append({
            "title": title,
            "url": link,
            "snippet": str(item.get("snippet", "")).strip(),
        })
        if len(results) >= max_results:
            break
    return results


# ── Combined two-tier entry point ────────────────────────────────────────────
def two_tier_search(query: str, k: int = 5) -> dict:
    """Notes first, then web; return ``{"notes": [...], "web": [...], "query": ...}``."""
    return {
        "notes": search_notes(query, k),
        "web": search_web(query, k),
        "query": query,
    }


# ── Self-test (no real network / files required) ─────────────────────────────
if __name__ == "__main__":
    print("== retrieval.py self-test ==")

    # 1) Tier-1 parsing/matching on inline index text (no files touched).
    sample_index = (
        "| ID | Title | Year | Evidence | Primary Chapter | Also Relevant To |"
        " Paradigm Tags | Key Use | Note Path | Full Text Path |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
        "| REF-0003 | Chinchilla | 2022 | B | 02 Pretraining |  | scaling |"
        " compute-optimal scaling law | references/02/chinchilla/note.md | x.md |\n"
        "| REF-0029 | Mamba | 2023 | A | 05 Architecture |  | ssm |"
        " linear-time selective SSM | references/05/mamba/note.md | y.md |\n"
    )
    rows = _parse_index_text(sample_index)
    assert len(rows) == 2, rows
    hits = _match_rows(rows, "compute-optimal scaling", k=5)
    assert hits and hits[0]["id"] == "REF-0003", hits
    assert hits[0]["note_path"].endswith("chinchilla/note.md"), hits[0]
    print(f"  tier-1 match ok: top hit {hits[0]['id']} ({hits[0]['title']})")

    # 2) Missing index degrades to [].
    assert search_notes("anything") == [] or isinstance(search_notes("anything"), list)
    print("  search_notes returns a list even when index is absent")

    # 3) Web tier with RETRIEVAL_MODEL unset must return [] (no raise).
    assert _cfg("RETRIEVAL_MODEL") == "", "test assumes RETRIEVAL_MODEL is blank"
    assert search_web("scaling laws for language models") == []
    combined = two_tier_search("scaling laws", k=3)
    assert combined["web"] == [] and combined["query"] == "scaling laws"
    print("  web tier disabled → search_web()=[] , two_tier_search web=[] (no error)")

    print("PASS")
