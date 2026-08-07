"""Missing-citation detection and supplementation.

Built on ``retrieval.py``. The project's iron rule: any citation that would enter
the bibliography from the web tier MUST carry a verifiable (reachable) URL — no
fabrication. Web candidates that fail URL verification are never inserted; they
are routed to ``needs_human`` instead.

Flow per uncited claim:
  1. Try the local bibliography (Tier 1): a note hit whose id is in ``bib_keys``
     is inserted as ``source="bib"``.
  2. Only if ``allow_web`` is true, try the web tier (Tier 2). Each candidate must
     pass ``verify_url`` before it can be inserted as ``source="web"``.
  3. Otherwise the claim goes to ``needs_human``.

``allow_web`` defaults to ``config.AUTO_CITE_WEB``.
"""
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Make sibling ``retrieval`` and project ``config`` importable whether this runs
# as a package module or as a standalone script.
sys.path.insert(0, str(Path(__file__).resolve().parent))          # agents/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # project root

import retrieval  # noqa: E402

try:  # pragma: no cover - trivial import guard
    import config as _config
except Exception:  # pragma: no cover
    _config = None

import os  # noqa: E402

URL_TIMEOUT = 8           # seconds for URL reachability check
_MIN_SENTENCE_LEN = 40    # skip trivial fragments (宁缺毋滥)
_USER_AGENT = "paper-agent-citation/1.0"

# ── Heuristic triggers (kept deliberately narrow) ────────────────────────────
_NUMERIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*%"                       # 17% / 12.5 %
    r"|\d+(?:\.\d+)?\s*(?:x|×)\b"              # 3x / 2.5×
    r"|\d+(?:\.\d+)?\s*(?:B|M|K|T)\b"          # 176B, 15T (params/tokens)
    r"|\bSOTA\b",
    re.IGNORECASE,
)
_COMPARATIVE_RE = re.compile(
    r"\b(?:outperform(?:s|ed)?|state[- ]of[- ]the[- ]art|surpass(?:es|ed)?"
    r"|compared\s+(?:to|with)|better\s+than|superior\s+to|exceeds?|beats?)\b",
    re.IGNORECASE,
)
_PRIORWORK_RE = re.compile(
    r"\b(?:prior\s+work|previous\s+work|recent\s+work|studies\s+(?:show|have)"
    r"|it\s+has\s+been\s+shown|have\s+shown|as\s+shown\s+in\s+the\s+literature"
    r"|widely\s+(?:used|adopted)|is\s+known\s+to)\b",
    re.IGNORECASE,
)
_CITE_RE = re.compile(r"\\cite|\\citep|\\citet", re.IGNORECASE)


def _paragraphs(text: str):
    """Yield ``(paragraph_text, absolute_offset)`` split on blank lines."""
    for match in re.finditer(r"(.+?)(?:\n[ \t]*\n|\Z)", text, re.DOTALL):
        yield match.group(1), match.start(1)


def _sentences(paragraph: str):
    """Yield ``(sentence_text, offset_within_paragraph)``.

    Splits on sentence-final punctuation followed by space/end. Decimals like
    ``17.3%`` are not split because the period is not followed by whitespace.
    """
    for match in re.finditer(r"[^.!?]*[.!?]+(?=\s|$)|\S[^.!?]*\Z", paragraph):
        chunk = match.group(0)
        if chunk.strip():
            yield chunk.strip(), match.start()


def _trigger_reason(sentence: str) -> str | None:
    if _NUMERIC_RE.search(sentence):
        return "numeric assertion without citation"
    if _COMPARATIVE_RE.search(sentence):
        return "comparative/superlative claim without citation"
    if _PRIORWORK_RE.search(sentence):
        return "reference to prior work without citation"
    return None


def find_uncited_claims(text: str) -> list[dict]:
    """Find sentences that look like they need a citation but have none nearby.

    Conservative by design: a sentence is flagged only when (a) its paragraph
    contains no ``\\cite*`` command, (b) it is at least ``_MIN_SENTENCE_LEN``
    chars, and (c) it matches a numeric / comparative / prior-work trigger.

    Returns ``[{"sentence","reason","offset"}]`` with ``offset`` an absolute
    index into ``text``.
    """
    claims = []
    for paragraph, para_offset in _paragraphs(text):
        if _CITE_RE.search(paragraph):
            continue  # already cited somewhere in this paragraph
        for sentence, sent_offset in _sentences(paragraph):
            if len(sentence) < _MIN_SENTENCE_LEN:
                continue
            reason = _trigger_reason(sentence)
            if reason:
                claims.append({
                    "sentence": sentence,
                    "reason": reason,
                    "offset": para_offset + sent_offset,
                })
    return claims


def verify_url(url: str) -> bool:
    """Return True if ``url`` is reachable (HTTP 2xx/3xx), else False.

    Tries HEAD first, falls back to GET if the server rejects HEAD. 8s timeout.
    Any exception (bad URL, DNS, timeout, TLS, 4xx/5xx) yields False — never
    raises. Redirects are followed by urllib, so a final 2xx/3xx passes.
    """
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return False

    def _try(method: str) -> bool:
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=URL_TIMEOUT) as response:
            return 200 <= getattr(response, "status", 200) < 400

    try:
        return _try("HEAD")
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 405, 501):  # HEAD not allowed → retry with GET
            try:
                return _try("GET")
            except Exception:
                return False
        return 200 <= exc.code < 400
    except Exception:
        return False


def _resolve_allow_web(allow_web) -> bool:
    if allow_web is not None:
        return bool(allow_web)
    if _config is not None and hasattr(_config, "AUTO_CITE_WEB"):
        return bool(_config.AUTO_CITE_WEB)
    return os.getenv("AUTO_CITE_WEB", "false").lower() == "true"


def _bib_match(query: str, bib_keys: set[str]) -> str | None:
    """Return an existing bib key relevant to ``query``, or None."""
    for hit in retrieval.search_notes(query, k=5):
        if hit.get("id") in bib_keys:
            return hit["id"]
    return None


def supplement_citations(text: str, bib_keys: set[str], allow_web: bool = None) -> dict:
    """Suggest citations for uncited claims.

    For each claim: prefer an existing local bib key; only if ``allow_web`` (which
    defaults to ``config.AUTO_CITE_WEB``) may the web tier be consulted, and every
    web candidate must pass ``verify_url`` before it is inserted. Unresolved (or
    unverifiable) claims are routed to ``needs_human``.

    Returns::

        {"insertions": [{"sentence","suggested_key_or_url","source":"bib|web"}],
         "needs_human": [{"sentence","reason"}]}
    """
    allow_web = _resolve_allow_web(allow_web)
    bib_keys = bib_keys or set()

    insertions, needs_human = [], []
    for claim in find_uncited_claims(text):
        sentence = claim["sentence"]

        # Tier 1: authoritative local bibliography.
        key = _bib_match(sentence, bib_keys)
        if key:
            insertions.append({
                "sentence": sentence,
                "suggested_key_or_url": key,
                "source": "bib",
            })
            continue

        # Tier 2: web — gated + every candidate must verify.
        if not allow_web:
            needs_human.append({
                "sentence": sentence,
                "reason": "no local bib match; web supplementation disabled",
            })
            continue

        verified_url = None
        for candidate in retrieval.search_web(sentence, max_results=5):
            if verify_url(candidate.get("url", "")):
                verified_url = candidate["url"]
                break
        if verified_url:
            insertions.append({
                "sentence": sentence,
                "suggested_key_or_url": verified_url,
                "source": "web",
            })
        else:
            needs_human.append({
                "sentence": sentence,
                "reason": "no verifiable URL from web tier (unreachable or none returned)",
            })

    return {"insertions": insertions, "needs_human": needs_human}


def apply_insertions(text: str, insertions: list[dict]) -> tuple:
    """Apply citation insertions to a document: put ``\\cite{key}`` at the end of
    each flagged sentence. Only bib-sourced insertions (``source == "bib"``) are
    applied — web-sourced URLs are not valid \\cite keys and are left to needs_human
    (this framework's contract: references are user-provided, never fabricated).

    Returns ``(new_text, applied_count, skipped)``. ``skipped`` lists insertions
    whose sentence was not found verbatim in ``text`` (already edited, or whitespace
    drift) — the caller should surface them rather than corrupt the document.
    """
    new_text = text
    applied = 0
    skipped = []
    for ins in insertions:
        if ins.get("source") != "bib" or not ins.get("suggested_key_or_url"):
            skipped.append({**ins, "reason": "not a local-bib key; left to human"})
            continue
        sentence = ins.get("sentence", "")
        key = ins.get("suggested_key_or_url", "")
        if not sentence or not key:
            skipped.append({**ins, "reason": "missing sentence or key"})
            continue
        if sentence in new_text:
            new_text = new_text.replace(sentence, sentence + f" \\cite{{{key}}}", 1)
            applied += 1
        else:
            skipped.append({**ins, "reason": "sentence not found verbatim in document"})
    return new_text, applied, skipped


# ── Self-test (no real network / files required) ─────────────────────────────
if __name__ == "__main__":
    print("== citation_supplement.py self-test ==")

    # 1) find_uncited_claims heuristics.
    sample = (
        "This section introduces the method and its motivation clearly.\n\n"
        "Our approach improves accuracy by 17% over the baseline on the test set.\n\n"
        "The model outperforms all prior systems on every benchmark we tried.\n\n"
        "This established result is well known and cited here \\cite{smith2020} already.\n\n"
        "The training corpus contains 15T tokens drawn from filtered web text."
    )
    claims = find_uncited_claims(sample)
    reasons = {c["reason"] for c in claims}
    sentences = [c["sentence"] for c in claims]
    assert any("17%" in s for s in sentences), sentences
    assert any("outperforms" in s for s in sentences), sentences
    assert any("15T tokens" in s for s in sentences), sentences
    # The \cite paragraph must be skipped, and the plain intro line ignored.
    assert not any("well known" in s for s in sentences), sentences
    assert not any("introduces the method" in s for s in sentences), sentences
    # Offsets point at the real text.
    for c in claims:
        assert sample[c["offset"]:].startswith(c["sentence"][:10]), c
    print(f"  find_uncited_claims: {len(claims)} claims, reasons={sorted(reasons)}")

    # 2) verify_url handles junk without raising / hitting the network.
    assert verify_url("not-a-real-url") is False
    assert verify_url("") is False
    assert verify_url("ftp://example.com/x") is False
    print("  verify_url rejects malformed URLs (no exceptions)")

    # 3) bib matching via monkeypatched Tier-1 (inline data, allow_web=False).
    retrieval.search_notes = lambda q, k=5: [
        {"id": "REF-0003", "title": "Chinchilla",
         "note_path": "references/02/chinchilla/note.md",
         "snippet": "compute-optimal scaling; 15T tokens; outperforms baselines"}
    ]
    res = supplement_citations(sample, bib_keys={"REF-0003"}, allow_web=False)
    bib_ins = [i for i in res["insertions"] if i["source"] == "bib"]
    assert bib_ins and all(i["suggested_key_or_url"] == "REF-0003" for i in bib_ins), res
    assert all(i["source"] != "web" for i in res["insertions"]), res
    print(f"  bib match: {len(bib_ins)} insertion(s) → REF-0003 (source=bib)")

    # 4) allow_web=False with no bib match → needs_human, never fabricated.
    retrieval.search_notes = lambda q, k=5: []
    res2 = supplement_citations(sample, bib_keys=set(), allow_web=False)
    assert res2["insertions"] == [], res2
    assert len(res2["needs_human"]) == len(claims), res2
    print(f"  no bib + web off → {len(res2['needs_human'])} needs_human, 0 insertions")

    # 5) allow_web default falls back to config.AUTO_CITE_WEB (False here) →
    #    web tier stays off, so no fabricated citations.
    res3 = supplement_citations(sample, bib_keys=set())
    assert res3["insertions"] == [], res3
    print("  allow_web default honors config.AUTO_CITE_WEB (web disabled → 0 insertions)")

    print("PASS")
