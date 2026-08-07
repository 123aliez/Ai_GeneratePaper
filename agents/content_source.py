"""Content-source abstraction — the seam that lets one Draft/Review skeleton
write either a survey (evidence = literature notes) or an experiment paper
(evidence = a results store: CSV/JSON/logs/plots).

`PAPER_MODE` in config picks the backend. Everything above this module (the
orchestrator, the agents) asks for a "context pack" for a section and does not
care where the evidence came from.
"""
import csv
import json
import os
import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import PAPER_MODE, DATA_ROOT, REFERENCES_ROOT, IDEA_PATH
from .chapter_type import IDEA, DATA, MIXED


# ── Results store (experiment mode) ─────────────────────────────────────
def _flatten_json(obj, prefix: str = "") -> dict:
    """Recursively flatten nested dicts to dotted numeric leaves."""
    flat = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(_flatten_json(value, f"{prefix}{key}." if not prefix else f"{prefix}{key}."))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            flat.update(_flatten_json(value, f"{prefix}{i}."))
    else:
        try:
            flat[prefix.rstrip(".")] = float(obj)
        except (TypeError, ValueError):
            pass
    return flat


def load_results_store(data_root=None) -> dict:
    """Scan data_root for *.json / *.csv and flatten to {metric: float}.

    Prefers number_gate.load_results_store if that module is present (so the
    two stay in sync); falls back to this local loader otherwise.
    """
    data_root = str(data_root or DATA_ROOT)
    try:
        from .number_gate import load_results_store as ng_loader
        return ng_loader(data_root)
    except Exception:
        pass
    store = {}
    root = Path(data_root)
    if not root.is_dir():
        return store
    for path in root.rglob("*.json"):
        if ".example." in path.name or path.name.startswith("_template"):
            continue
        try:
            store.update(_flatten_json(json.loads(path.read_text(encoding="utf-8", errors="replace")),
                                       prefix=f"{path.stem}."))
        except Exception:
            continue
    for path in root.rglob("*.csv"):
        if ".example." in path.name or path.name.startswith("_template"):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) >= 2:
                        try:
                            store[f"{path.stem}.{row[0].strip()}"] = float(row[1])
                        except (ValueError, IndexError):
                            continue
        except Exception:
            continue
    return store


def list_plots(data_root=None) -> list[str]:
    """Return available plot filenames (png/pdf/jpg) under data_root/plots or data_root."""
    data_root = Path(data_root or DATA_ROOT)
    plots = []
    for pattern in ("*.png", "*.pdf", "*.jpg", "*.jpeg"):
        plots.extend(str(p.relative_to(data_root)) for p in data_root.rglob(pattern))
    return sorted(plots)


def _format_results_table(store: dict, limit: int = 120) -> str:
    """Render numeric metrics as a table and textual metadata as a list.

    Textual metadata (run_name, description, hardware, dataset, hyperparameters)
    is stored with a leading ":" marker; it is NOT a citable number and is shown
    as run context, separate from the numeric table the number gate checks."""
    numeric = {k: v for k, v in store.items() if not (isinstance(v, str) and v.startswith(":"))}
    textual = {k: v[1:] for k, v in store.items() if isinstance(v, str) and v.startswith(":")}

    blocks = []
    if textual:
        blocks.append("## Run metadata (context — NOT numbers to cite)")
        blocks.append("\n".join(f"- **{k}**: {v}" for k, v in sorted(textual.items())))
        blocks.append("")
    if numeric:
        rows = ["| Metric | Value |", "|---|---:|"]
        for key in sorted(numeric)[:limit]:
            rows.append(f"| {key} | {numeric[key]} |")
        if len(numeric) > limit:
            rows.append(f"| … | ({len(numeric) - limit} more) |")
        blocks.append("## Results table (numbers — these are the ONLY citable values)")
        blocks.append("\n".join(rows))
    else:
        blocks.append("(no numeric results found in data/ — provide CSV/JSON results first)")
    return "\n\n".join(blocks)


# ── Idea document (idea-family chapters) ────────────────────────────────
def load_idea_document(idea_path=None) -> str:
    """Read the user's global idea document, or "" if absent.

    This is the primary input for Method / Introduction / Related Work: the
    novelty and mechanism the paper argues for. Deliberately passed through
    whole rather than summarized — the drafter needs the user's own framing of
    the contribution, and paraphrasing it upstream is how a paper's claims drift.
    """
    path = Path(idea_path or IDEA_PATH)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


# Minimum words of actual authored prose before an idea document counts as
# filled in. A skeleton copied from the template is mostly `>` prompt blocks and
# empty table rows; stripping those leaves almost nothing. 40 words is low enough
# that a terse one-line-per-section answer passes, high enough that an untouched
# skeleton does not.
_IDEA_MIN_WORDS = 40


def idea_is_skeleton(idea_text: str) -> tuple[bool, int]:
    """Detect an idea document that exists but was never filled in.

    Returns (is_skeleton, authored_word_count).

    The pre-flight gate checks that idea.md *exists*; without this it would also
    accept a file that is 100% template — the worst case, because the run then
    proceeds and the drafter treats the template's own questions as content.
    Everything that is scaffolding is stripped: `>` prompt blocks, headings,
    table rules, empty table rows, and bare `- $$:` symbol stubs.
    """
    if not idea_text:
        return True, 0
    if re.search(r"状态\s*[:：]\s*未填写|status\s*[:：]\s*(not filled|todo|unfilled)",
                 idea_text, re.IGNORECASE):
        return True, 0
    authored = []
    for line in idea_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((">", "#", "---")):
            continue                                  # prompts, headings, rules
        if set(stripped) <= set("|-: "):
            continue                                  # table rule / empty row
        if stripped.startswith("|") and not re.sub(r"[|\s]", "", stripped):
            continue                                  # blank table row
        if re.fullmatch(r"[-*+]?\s*\$*\s*\$*\s*[:：]?\s*", stripped):
            continue                                  # unfilled `- $$:` stub
        if re.fullmatch(r"[-*+]\s*", stripped):
            continue                                  # empty bullet
        if re.fullmatch(r"\d+\.\s*", stripped):
            continue                                  # empty numbered item
        authored.append(stripped)
    words = len(" ".join(authored).split())
    # Chinese prose has few spaces; count CJK characters as words too.
    words += len(re.findall(r"[一-鿿]", " ".join(authored)))
    return words < _IDEA_MIN_WORDS, words


def _idea_block(section_title: str, idea_text: str) -> list[str]:
    """The idea-document half of a context pack."""
    skeleton, words = idea_is_skeleton(idea_text)
    if idea_text and skeleton:
        return [
            "## Core idea (TEMPLATE NOT FILLED IN)",
            "",
            f"An idea document exists at {IDEA_PATH} but contains only the template "
            f"skeleton ({words} words of authored text). The `>` blocks in it are "
            "questions addressed to the author, NOT content for the paper.",
            "",
            "Do NOT answer those questions yourself and do NOT treat them as the",
            "paper's claims. Write [IDEA NEEDED] where the contribution belongs and",
            "record in todo.md that the idea document needs to be written.",
        ]
    if not idea_text:
        return [
            "## Core idea (MISSING)",
            "",
            f"No idea document was found at {IDEA_PATH}. This section's primary",
            "input is the paper's novelty and method design, which only the author",
            "can state. Do NOT invent a contribution: write [IDEA NEEDED] where the",
            "novelty claim belongs and note it in todo.md.",
        ]
    return [
        "## Core idea — THE PRIMARY SOURCE FOR THIS SECTION",
        "",
        "The text below is the author's own statement of the contribution: the",
        "novelty, the mechanism, and the method design. Write this section from it.",
        "Do not water down or re-invent the claim; do not add a contribution the",
        "author did not make. Where a design detail is absent, write",
        "[DESIGN DETAIL NEEDED] rather than filling it in plausibly.",
        "",
        idea_text,
    ]


def _results_block(section_title: str, store: dict, plots: list[str],
                   primary: bool) -> list[str]:
    """The results-store half of a context pack.

    `primary` distinguishes a Results chapter (numbers ARE the content) from an
    idea chapter that may quote one headline number for motivation.
    """
    if primary:
        head = [
            "## Experiment results — THE PRIMARY SOURCE FOR THIS SECTION",
            "",
            "You may ONLY state numbers that appear in the results table below. Do not",
            "invent, round beyond the given precision, or extrapolate. Do not describe",
            "hardware, hyperparameters, or dataset sizes not present in data/. If a",
            "needed value is absent, write [MISSING DATA] rather than guessing.",
        ]
    else:
        head = [
            "## Experiment results — SUPPORTING EVIDENCE ONLY",
            "",
            "This section is about the idea, not the numbers. Quote a value here only",
            "when it directly supports a claim about the contribution (e.g. one",
            "headline result for motivation). Every number you do write must appear",
            "verbatim in the table below; leave the detailed reporting to the results",
            "section. If a value is absent, write [MISSING DATA] rather than guessing.",
        ]
    return head + [
        "",
        _format_results_table(store),
        "",
        "## Available plots (reference by filename; do not invent figures)",
        ("\n".join(f"- {p}" for p in plots) if plots else "(none provided)"),
    ]


# ── Context pack ────────────────────────────────────────────────────────
def build_context_pack(section_title: str = "", data_root=None,
                       family: str = DATA, idea_path=None) -> str:
    """Build the evidence block injected into a Draft prompt for one section.

    `family` (from chapter_type) decides what the pack leads with:

    * ``idea``  — the idea document is primary; the results table is appended as
                  clearly-labelled supporting evidence so a Method chapter can
                  still quote one headline number without inventing it.
    * ``data``  — the results store is primary; the idea document is appended as
                  context so the results are narrated against the actual claim.
    * ``mixed`` — both presented as primary (discussion/conclusion).

    Survey mode defers to the reference excerpt the orchestrator injects.
    """
    if PAPER_MODE != "experiment":
        return ""  # survey mode uses the orchestrator's reference excerpt

    idea_text = load_idea_document(idea_path)
    store = load_results_store(data_root)
    plots = list_plots(data_root)

    lines = [f"# Context pack for: {section_title or '(this section)'}",
             f"Evidence routing: {family}", ""]
    if family == IDEA:
        lines += _idea_block(section_title, idea_text)
        lines += ["", *_results_block(section_title, store, plots, primary=False)]
    elif family == DATA:
        lines += _results_block(section_title, store, plots, primary=True)
        if idea_text:
            lines += [
                "", "## Core idea — CONTEXT for interpreting the results",
                "",
                "Narrate the numbers against this claim; do not restate the method",
                "design in full (it belongs to the method section).",
                "", idea_text,
            ]
    else:
        lines += _idea_block(section_title, idea_text)
        lines += ["", *_results_block(section_title, store, plots, primary=True)]
    return "\n".join(lines) + "\n"


def content_source_summary(family: str = "") -> str:
    """One-line description of the active content source (for run banners)."""
    if PAPER_MODE != "experiment":
        return f"survey mode — reference notes in {REFERENCES_ROOT}"
    numeric = {k: v for k, v in load_results_store().items()
               if not (isinstance(v, str) and v.startswith(":"))}
    idea = load_idea_document()
    idea_state = f"idea.md {len(idea.split())} words" if idea else "idea.md MISSING"
    parts = [f"experiment mode — {idea_state}", f"{len(numeric)} metrics in {DATA_ROOT.name}/"]
    if family:
        parts.append(f"routing={family}")
    return " — ".join(parts[:1]) + ", " + ", ".join(parts[1:])


if __name__ == "__main__":
    print("PAPER_MODE:", PAPER_MODE)
    print("summary:", content_source_summary())
    for fam in (IDEA, DATA, MIXED):
        pack = build_context_pack("Method", family=fam)
        head = pack.splitlines()[:6]
        print(f"\n--- family={fam} ---")
        print("\n".join(head))
        assert f"Evidence routing: {fam}" in pack
    # An idea chapter must never present the results table as primary.
    idea_pack = build_context_pack("Method", family=IDEA)
    assert "SUPPORTING EVIDENCE ONLY" in idea_pack, "idea chapters must demote the results table"
    data_pack = build_context_pack("Results", family=DATA)
    assert "THE PRIMARY SOURCE" in data_pack.split("## Core idea")[0]
    print("\nSELF-TEST PASSED: context packs route by family.")
