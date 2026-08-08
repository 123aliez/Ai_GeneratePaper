"""Chapter-type routing — decides WHAT a chapter is written from.

The core correction over the framework's first design: a paper's contribution is
the *idea* (novelty, mechanism, method design). Experiment numbers only support
it. So a Method / Introduction / Related Work chapter must be drafted from the
user's `idea.md`, and only Experiments / Results chapters read the results store
under `data/`. Applying the results store (and its fail-closed
number gate) to every chapter was an architecture-level mistake: it starved the
idea chapters of their real source and blocked runs that legitimately have no
numbers yet.

Two things are resolved here:

* **family** — which evidence source feeds the draft:
    - ``idea``  : `idea.md` is the primary input (method/intro/related/abstract)
    - ``data``  : the results store under `data/` is primary (experiments/results)
    - ``mixed`` : both (discussion/conclusion, and the unknown-type fallback)

* **gate** — how the deterministic number gate behaves:
    - ``blocking``  : no results store => abort. Numbers ARE the content.
    - ``advisory``  : check if a store exists, report mismatches as MUST FIX,
                      but never block on an absent store.
    - ``off``       : do not run the gate (pure prose, e.g. Related Work).

Declared explicitly in `brief.md` (`type: method`), per section if needed. No
guessing from folder names unless the user left the type out entirely.
"""

IDEA = "idea"
DATA = "data"
MIXED = "mixed"

BLOCKING = "blocking"
ADVISORY = "advisory"
OFF = "off"

# Canonical chapter types → (family, gate). Order matters only for reporting.
CHAPTER_TYPES = {
    "abstract":     (IDEA, ADVISORY),
    "intro":        (IDEA, ADVISORY),
    "related":      (IDEA, OFF),
    "background":   (IDEA, OFF),
    "method":       (IDEA, ADVISORY),
    "theory":       (IDEA, ADVISORY),
    "experiments":  (DATA, BLOCKING),
    "results":      (DATA, BLOCKING),
    "analysis":     (DATA, BLOCKING),
    "ablation":     (DATA, BLOCKING),
    "discussion":   (MIXED, ADVISORY),
    "limitations":  (MIXED, ADVISORY),
    "conclusion":   (MIXED, ADVISORY),
}

DEFAULT_TYPE = "unknown"
DEFAULT_ROUTE = (MIXED, ADVISORY)

# Spellings a user might reasonably write in brief.md, mapped to canonical types.
# Kept generous on purpose: a typo silently falling back to `unknown` is worse
# than accepting a synonym, because `unknown` routes to mixed/advisory and the
# chapter quietly loses its intended gate.
ALIASES = {
    "abstract": "abstract", "summary": "abstract", "摘要": "abstract",
    "introduction": "intro", "intro": "intro", "引言": "intro", "绪论": "intro",
    "related": "related", "related work": "related", "related-work": "related",
    "relatedwork": "related", "literature": "related", "相关工作": "related",
    "background": "background", "preliminaries": "background",
    "preliminary": "background", "背景": "background", "预备知识": "background",
    "method": "method", "methods": "method", "methodology": "method",
    "approach": "method", "model": "method", "architecture": "method",
    "design": "method", "方法": "method", "模型": "method",
    "theory": "theory", "theoretical": "theory", "analysis-theory": "theory",
    "proof": "theory", "理论": "theory",
    "experiments": "experiments", "experiment": "experiments",
    "experimental setup": "experiments", "setup": "experiments",
    "实验": "experiments", "实验设置": "experiments",
    "results": "results", "result": "results", "evaluation": "results",
    "eval": "results", "结果": "results", "评估": "results",
    "analysis": "analysis", "empirical analysis": "analysis", "分析": "analysis",
    "ablation": "ablation", "ablations": "ablation",
    "ablation study": "ablation", "消融": "ablation", "消融实验": "ablation",
    "discussion": "discussion", "讨论": "discussion",
    "limitations": "limitations", "limitation": "limitations",
    "threats": "limitations", "局限": "limitations", "不足": "limitations",
    "conclusion": "conclusion", "conclusions": "conclusion",
    "future work": "conclusion", "结论": "conclusion", "总结": "conclusion",
}


def normalize_type(raw: str) -> str:
    """Map a user-written type string to a canonical chapter type.

    Unrecognized values return DEFAULT_TYPE rather than raising: an unfamiliar
    label should degrade to the safe mixed/advisory route and be reported, not
    kill the run.
    """
    if not raw:
        return DEFAULT_TYPE
    key = " ".join(str(raw).strip().lower().replace("_", " ").split())
    if key in CHAPTER_TYPES:
        return key
    if key in ALIASES:
        return ALIASES[key]
    # Tolerate decorated values like "type: method (核心章)" or "Method chapter".
    for token in key.replace("/", " ").replace(",", " ").split():
        if token in CHAPTER_TYPES:
            return token
        if token in ALIASES:
            return ALIASES[token]
    return DEFAULT_TYPE


def route_for_type(chapter_type: str) -> tuple[str, str]:
    """Return (family, gate) for a canonical or raw type string."""
    return CHAPTER_TYPES.get(normalize_type(chapter_type), DEFAULT_ROUTE)


import os
import re
from pathlib import Path

# `type: method`, `- type: method`, `**type**: method`, `类型: 方法`. Anchored to
# line start so a `type:` mentioned inside prose is not picked up.
_TYPE_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\*\*|__)?\s*(?:type|chapter[ _-]?type|类型|章节类型)\s*"
    r"(?:\*\*|__)?\s*[:：]\s*(.+?)\s*$",
    re.IGNORECASE,
)
# A numbered section heading in brief.md, matching parse_brief_sections' shape:
#   `3. **Method** (~300 words)`
_SECTION_RE = re.compile(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*", re.IGNORECASE)
# A fenced-code fence; `type:` inside a code block is illustrative, not a
# declaration (outline.example.md shows the syntax in a snippet).
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def parse_brief_type(folder_path) -> dict:
    """Read the chapter type declaration(s) out of `<folder>/brief.md`.

    Returns::

        {"declared": <raw string or "">,      # what the user actually wrote
         "type": <canonical type>,            # normalized, DEFAULT_TYPE if absent
         "family": "idea"|"data"|"mixed",
         "gate": "blocking"|"advisory"|"off",
         "source": "brief"|"folder"|"default",
         "sections": {<section number>: <canonical type>},  # per-section overrides
         "unrecognized": <raw string or "">}  # set when a value failed to map

    The first ``type:`` line before any numbered section is the chapter-level
    declaration. A ``type:`` line *inside* a numbered section overrides that
    section only, which is what lets a single-file paper (Abstract → Conclusion
    in one brief.md) route each section to the right evidence source.
    """
    info = {
        "declared": "", "type": DEFAULT_TYPE, "family": DEFAULT_ROUTE[0],
        "gate": DEFAULT_ROUTE[1], "source": "default", "sections": {},
        "unrecognized": "",
    }
    brief_path = Path(folder_path) / "brief.md"
    if not brief_path.exists():
        info["type"] = _type_from_folder_name(folder_path) or DEFAULT_TYPE
        if info["type"] != DEFAULT_TYPE:
            info["source"] = "folder"
        info["family"], info["gate"] = route_for_type(info["type"])
        return info

    current_section = None
    in_fence = False
    for line in brief_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            current_section = int(section_match.group(1))
            # A section heading may name its own type inline: `**Results** (type: results)`.
            inline = normalize_type(section_match.group(2))
            if inline != DEFAULT_TYPE:
                info["sections"][current_section] = inline
            continue
        type_match = _TYPE_LINE_RE.match(line)
        if not type_match:
            continue
        raw = type_match.group(1).strip().strip("`").strip()
        # Ignore the template's own placeholder (`type: <method|results|...>`).
        if raw.startswith("<") or not raw:
            continue
        canonical = normalize_type(raw)
        if current_section is None:
            if info["declared"]:
                continue  # first chapter-level declaration wins
            info["declared"] = raw
            info["type"] = canonical
            info["source"] = "brief"
            if canonical == DEFAULT_TYPE:
                info["unrecognized"] = raw
        elif canonical != DEFAULT_TYPE:
            info["sections"][current_section] = canonical

    if info["source"] != "brief":
        guessed = _type_from_folder_name(folder_path)
        if guessed:
            info["type"], info["source"] = guessed, "folder"

    info["family"], info["gate"] = route_for_type(info["type"])
    return info


def resolve_run_route(folder_path, sections=None) -> dict:
    """Resolve the effective route for a whole run, folding in per-section types.

    A brief.md that covers the entire paper (the common single-file case) has
    idea sections AND data sections. Blocking the run because *some* section
    needs numbers, or skipping the store because *some* section doesn't, would
    both be wrong. So the run-level route is the union:

    * family  — ``mixed`` when both idea-family and data-family sections exist.
    * gate    — ``blocking`` only if a data-family section is present (the paper
                genuinely cannot be written without numbers); otherwise the
                chapter-level gate, floored at ``advisory`` when any section
                could carry a number.

    `sections` is the list from ``parse_brief_sections`` (dicts with "number"
    and "title"); pass None to route on the chapter declaration alone.
    """
    info = parse_brief_type(folder_path)
    per_section = dict(info["sections"])

    # Infer a type for sections the user did not annotate, from their titles.
    for section in (sections or []):
        number = section.get("number")
        if number in per_section:
            continue
        guessed = normalize_type(section.get("title", ""))
        if guessed != DEFAULT_TYPE:
            per_section[number] = guessed

    info["section_types"] = per_section
    families = {route_for_type(t)[0] for t in per_section.values()}
    gates = {route_for_type(t)[1] for t in per_section.values()}

    if not families:
        info["families"] = {info["family"]}
        return info

    # Union the families; a run touching both idea and data chapters is mixed.
    if families == {IDEA}:
        family = IDEA if info["family"] in (IDEA, MIXED) else info["family"]
    elif families == {DATA}:
        family = DATA if info["family"] in (DATA, MIXED) else MIXED
    else:
        family = MIXED
    if info["source"] == "brief" and info["family"] != MIXED and families - {info["family"]}:
        family = MIXED  # explicit chapter type plus off-type sections

    if BLOCKING in gates:
        gate = BLOCKING
    elif info["gate"] == BLOCKING:
        gate = BLOCKING
    elif ADVISORY in gates or info["gate"] == ADVISORY:
        gate = ADVISORY
    else:
        gate = OFF

    info["family"], info["gate"], info["families"] = family, gate, families
    return info


def route_banner(info: dict) -> str:
    """One-line, human-readable summary of the resolved route for run output."""
    source = {"brief": "brief.md", "folder": "folder name", "default": "default"}.get(
        info.get("source", "default"), info.get("source"))
    parts = [f"type={info.get('type', DEFAULT_TYPE)} (from {source})",
             f"evidence={info.get('family')}",
             f"number-gate={info.get('gate')}"]
    section_types = info.get("section_types") or info.get("sections") or {}
    if section_types:
        parts.append("sections=" + ",".join(
            f"{n}:{t}" for n, t in sorted(section_types.items())))
    return " | ".join(parts)


def _type_from_folder_name(folder_path) -> str:
    """Last-resort inference from the workspace folder name (`02-method` → method).

    Only used when brief.md declares nothing. Longest alias first so
    `related work` beats `work`, and `ablation study` beats `study`.
    """
    name = os.path.basename(str(folder_path).replace("\\", "/").rstrip("/"))
    haystack = " ".join(re.split(r"[\s_\-.]+", name.lower()))
    for alias in sorted(ALIASES, key=len, reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", haystack):
            return ALIASES[alias]
    return ""


# ── Self-test (no API, no real files beyond a temp dir) ──────────────────
if __name__ == "__main__":
    import tempfile

    def write_brief(text: str) -> str:
        folder = tempfile.mkdtemp()
        Path(folder, "brief.md").write_text(text, encoding="utf-8")
        return folder

    # 1. Explicit chapter-level declaration.
    f = write_brief("# Brief: Method\n\ntype: method\n\n## Task\n...\n")
    info = parse_brief_type(f)
    assert (info["type"], info["family"], info["gate"]) == ("method", IDEA, ADVISORY), info
    assert info["source"] == "brief"

    # 2. Aliases and decorated forms.
    for raw, want in [("Results", "results"), ("**type**: Ablation Study", "ablation"),
                      ("类型: 方法", "method"), ("related-work", "related")]:
        line = raw if raw.startswith("**") else f"type: {raw}"
        got = parse_brief_type(write_brief(f"# B\n\n{line}\n"))["type"]
        assert got == want, (raw, got, want)

    # 3. Data chapter blocks; prose chapter turns the gate off entirely.
    assert parse_brief_type(write_brief("type: results\n"))["gate"] == BLOCKING
    assert parse_brief_type(write_brief("type: related work\n"))["gate"] == OFF

    # 4. No declaration → fall back to the folder name.
    folder = tempfile.mkdtemp(suffix="_05-results")
    Path(folder, "brief.md").write_text("# B\n\n## Task\n...\n", encoding="utf-8")
    info = parse_brief_type(folder)
    assert (info["type"], info["source"]) == ("results", "folder"), info

    # 5. Unrecognized value degrades to mixed/advisory and is reported.
    info = parse_brief_type(write_brief("type: quux\n"))
    assert info["type"] == DEFAULT_TYPE and info["unrecognized"] == "quux", info
    assert (info["family"], info["gate"]) == (MIXED, ADVISORY)

    # 6. `type:` inside a fenced block is illustrative, not a declaration.
    info = parse_brief_type(write_brief("# B\n\n```\ntype: results\n```\n\ntype: method\n"))
    assert info["type"] == "method", info

    # 7. Whole-paper brief: idea + data sections union to mixed/blocking.
    whole = write_brief(
        "# Brief: Full paper\n\n"
        "1. **Introduction** (~250 words)\n- ...\n\n"
        "4. **Method** (~300 words)\n- ...\n\n"
        "5. **Results** (~300 words)\n- ...\n"
    )
    sections = [{"number": 1, "title": "Introduction"},
                {"number": 4, "title": "Method"},
                {"number": 5, "title": "Results"}]
    info = resolve_run_route(whole, sections)
    assert info["family"] == MIXED, info
    assert info["gate"] == BLOCKING, info
    assert info["section_types"] == {1: "intro", 4: "method", 5: "results"}, info

    # 8. Idea-only paper part never blocks on a missing results store.
    idea_only = write_brief(
        "type: method\n\n1. **Method** (~300 words)\n- ...\n\n"
        "2. **Related Work** (~200 words)\n- ...\n"
    )
    info = resolve_run_route(idea_only, [{"number": 1, "title": "Method"},
                                         {"number": 2, "title": "Related Work"}])
    assert info["family"] == IDEA, info
    assert info["gate"] == ADVISORY, info

    # 9. Per-section override beats the title-derived guess.
    override = write_brief(
        "type: method\n\n1. **Deep Dive** (~300 words)\n- type: results\n- ...\n"
    )
    info = resolve_run_route(override, [{"number": 1, "title": "Deep Dive"}])
    assert info["section_types"] == {1: "results"}, info
    assert info["gate"] == BLOCKING, info

    print("route banner:", route_banner(info))
    print("SELF-TEST PASSED: type routing, aliases, fallbacks, section union.")
