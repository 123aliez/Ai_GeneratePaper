import contextlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path
from .tools import set_agent_context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import (
    MAX_REVISION_ROUNDS,
    REVIEW_SCORE_THRESHOLD,
    REFERENCE_INDEX_PATH as _REFERENCE_INDEX_PATH,
    PAPER_MODE,
    DATA_ROOT,
    IDEA_PATH,
)
from .chapter_type import (
    resolve_run_route, route_banner,
    IDEA, DATA, MIXED, BLOCKING, ADVISORY, OFF,
)

# Project-local reference index (config points it at this project's references/,
# not the survey's). Kept as a module global for build_reference_excerpt.
REFERENCE_INDEX_PATH = Path(_REFERENCE_INDEX_PATH)


PROGRESS_PATTERNS = [
    (re.compile(r"Stage\s+1|draft-v1|Stage 1", re.I), "[Manager] Stage 1/4 | Drafting draft-v1.md"),
    (re.compile(r"Stage\s+2|review-v1|Stage 2", re.I), "[Manager] Stage 2/4 | Reviewing draft-v1.md"),
    (re.compile(r"Stage\s+3|draft-v2|Stage 3", re.I), "[Manager] Stage 3/4 | Revising draft-v2.md"),
    (re.compile(r"Stage\s+4|final\.md|Stage 4", re.I), "[Manager] Stage 4/4 | Finalizing final.md and final.zh.md"),
    (re.compile(r"draft_agent", re.I), "[Manager] Agent     | Calling draft_agent"),
    (re.compile(r"review_agent", re.I), "[Manager] Agent     | Calling review_agent"),
]

TOOL_CALL_RE = re.compile(r"\b(read_file|write_file|list_folder)\s*\([^)]*(?:file_path|folder_path)\s*=\s*['\"]([^'\"]+)['\"]", re.S)
ERROR_RE = re.compile(r"\b(error|exception|failed|traceback|abort|warning)\b", re.I)
NOTICE_ERROR_RE = re.compile(r"Error in generating model output|litellm\.|AnthropicException|OpenAIException|BadGatewayError|MidStreamFallbackError|InternalServerError|ConnectError|Timeout|Server disconnected", re.I)
NOTICE_IGNORE_RE = re.compile(r"LiteLLM\.Info:\s*If you need to debug this error", re.I)
RAW_NOISE_RE = re.compile(r"^\s*(Thought:|<code>|Code execution failed at line|import\s|from\s|def\s|for\s|while\s|if\s|elif\s|else:|return\s|[A-Za-z_][A-Za-z0-9_]*\s*=)", re.I)
STRUCTURED_PROGRESS_RE = re.compile(r"(?:\[(?:Manager|Draft|Review)\]\s*)?(?:\[(?:Stage|Agent|Tool|Verify|Notice|Done|Error)\]|(?:Stage|Agent|Tool|Verify|Notice|Done|Error)\s*\|)[^\n\r]*")
LEGACY_PROGRESS_RE = re.compile(r"^\[(Manager|Draft|Review)\]\[(Stage|Agent|Tool|Verify|Notice|Done|Error)\]\s*(.*)$")
ABS_PATH_RE = re.compile(r"[A-Za-z]:[/\\][^\s'\"),]+")
TRANSIENT_ERROR_RE = re.compile(r"SSL:|UNEXPECTED_EOF|Connection error|ConnectError|Server disconnected|Timeout|timed out|InternalServerError", re.I)

REVIEW_JSON_SCHEMA_HINT = """{
  "scores": {"accuracy": 1-5, "completeness": 1-5, "clarity": 1-5, "structure": 1-5, "readability": 1-5, "ai_traces": 1-5, "style_consistency": 1-5, "overall": 1-5},
  "decision": "REVISE" or "ACCEPT",
  "must_fix": [{"id": "MF1", "location": "section/paragraph", "issue": "...", "suggestion": "..."}],
  "should_fix": [{"id": "SF1", "location": "...", "issue": "...", "suggestion": "..."}],
  "consider": [{"id": "C1", "note": "..."}],
  "needs_citation": [{"sentence": "the exact sentence needing a citation", "suggested_key": "REF-0003 or null"}]
}"""

VERIFY_JSON_SCHEMA_HINT = """{
  "all_resolved": true or false,
  "items": [{"id": "MF1", "resolved": true or false, "note": "how it was resolved, or what is still missing"}]
}"""


def display_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "/paper/"
    if marker in normalized:
        return "paper/" + normalized.split(marker, 1)[1]
    marker = "/survey/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    if ":/" in normalized:
        return Path(normalized).name
    return normalized


def sanitize_progress_text(text: str) -> str:
    return ABS_PATH_RE.sub(lambda match: display_path(match.group(0)), text)


def format_progress_message(message: str) -> str:
    message = sanitize_progress_text(" ".join(message.split()))
    legacy = LEGACY_PROGRESS_RE.match(message)
    if legacy:
        owner, kind, detail = legacy.groups()
        detail = detail.strip()
        if kind == "Tool":
            tool_match = re.match(r"(\w+)\s*:\s*(.*)", detail)
            if tool_match:
                return f"[{owner}] Tool      | {tool_match.group(1)} -> {tool_match.group(2)}"
        return f"[{owner}] {kind:<9} | {detail}"
    return message


def print_stream_progress(content: str, seen: set[str], owner: str = "Manager", output=None, include_stage_hints: bool = False) -> None:
    if not content:
        return
    output = output or None

    def emit(message: str) -> None:
        if output is None:
            print(message, flush=True)
        else:
            output.write(message + "\n")
            output.flush()

    text = " ".join(content.split())

    if include_stage_hints:
        for pattern, message in PROGRESS_PATTERNS:
            if pattern.search(content) and message not in seen:
                emit(message)
                seen.add(message)

    for tool_name, path in TOOL_CALL_RE.findall(content):
        message = f"[{owner:<8.8}] {tool_name:<10} | {display_path(path)}"
        if message not in seen:
            emit(message)
            seen.add(message)

    if RAW_NOISE_RE.search(text):
        return

    for raw_message in STRUCTURED_PROGRESS_RE.findall(content):
        message = format_progress_message(raw_message)
        if "Thought:" in message or "Code execution failed" in message or "<code>" in message:
            continue
        if message not in seen:
            emit(message)
            seen.add(message)
    if NOTICE_IGNORE_RE.search(text):
        return
    if NOTICE_ERROR_RE.search(content):
        if text and text not in seen:
            emit(f"[{owner:<8.8}] notice     | {sanitize_progress_text(text[:220])}")
            seen.add(text)


class ProgressFilteringStdout(io.StringIO):
    def __init__(self, owner: str, seen: set[str], output):
        super().__init__()
        self.owner = owner
        self.seen = seen
        self.output = output
        self.buffer = ""

    def write(self, text):
        self.buffer += str(text)
        print_stream_progress(str(text), self.seen, self.owner, self.output)
        return len(str(text))

    def flush(self):
        return None


def chapter_name_from_folder(folder_path: str) -> str:
    return Path(folder_path.replace("\\", "/")).name


def build_reference_excerpt(chapter: str) -> str:
    if not REFERENCE_INDEX_PATH.exists():
        return (
            f"# Reference index excerpt for {chapter}\n"
            f"(No reference index at {REFERENCE_INDEX_PATH.name}; "
            f"run generate_reference_index.py or work notes-free.)\n"
            f"Use search_references(query, chapter) only if an index exists.\n"
        )
    lines = REFERENCE_INDEX_PATH.read_text(encoding="utf-8").splitlines()
    rows = []
    total = 0
    for line in lines:
        if not line.startswith("| REF-"):
            continue
        total += 1
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) < 10:
            continue
        if columns[4] == chapter or chapter in columns[5]:
            rows.append(line)
    header = [
        f"# Reference index excerpt for {chapter}",
        f"Filter: Primary Chapter == \"{chapter}\" OR Also Relevant To contains \"{chapter}\"",
        f"Rows: {len(rows)} of {total}",
        "",
        "Evidence grades: A=peer-reviewed, B=arXiv/preprint, C=technical report/system card, D=blog/model card, E=third-party/weak source.",
        "Do not read paper/references/index.md directly; use this excerpt or bounded search_references for additional lookup.",
        "",
        "| ID | Title | Year | Evidence | Primary Chapter | Also Relevant To | Paradigm Tags | Key Use | Note Path | Full Text Path |",
        "|---|---|---:|:---:|---|---|---|---|---|---|",
    ]
    return "\n".join(header + rows) + "\n"


def parse_brief_sections(folder_path: str) -> list[dict]:
    brief_path = Path(folder_path) / "brief.md"
    if not brief_path.exists():
        return []
    sections = []
    current = None
    section_re = re.compile(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*\s+\(~?(\d+)\s+words?\)", re.I)
    for line in brief_path.read_text(encoding="utf-8").splitlines():
        match = section_re.match(line)
        if match:
            current = {
                "number": int(match.group(1)),
                "title": match.group(2).strip(),
                "target_words": int(match.group(3)),
                "bullets": [],
            }
            sections.append(current)
        elif current and line.strip().startswith("-"):
            current["bullets"].append(line.strip())
    return sections


def build_stage1_parts(sections: list[dict]) -> list[dict]:
    titles = [section["title"].lower() for section in sections]
    is_alignment = any("rlvr" in title for title in titles) and any("constitutional" in title for title in titles)
    if len(sections) >= 6 and is_alignment:
        groups = [[sections[0]], [sections[1], sections[3]], [sections[2], *sections[4:]]]
    elif len(sections) >= 6:
        groups = [sections[:2], sections[2:4], sections[4:]]
    elif len(sections) >= 3:
        size = (len(sections) + 2) // 3
        groups = [sections[:size], sections[size:size * 2], sections[size * 2:]]
    else:
        groups = [sections, [], []]

    parts = []
    for index, group in enumerate(groups, start=1):
        titles = [section["title"] for section in group]
        words = sum(section["target_words"] for section in group)
        numbers = [section["number"] for section in group]
        if not titles:
            titles = [f"Chapter part {index}"]
            words = 700
        parts.append({
            "index": index,
            "output": f"draft-v1.part-{index}.md",
            "titles": titles,
            "numbers": numbers,
            "target_words": words,
        })
    return parts


def part_family(part: dict, route: dict) -> str:
    """Resolve the evidence family for one Stage-1 part.

    A whole-paper brief splits across families — part 1 might be Abstract +
    Introduction (idea) while part 3 is Results + Conclusion (data). Giving all
    three parts the chapter-level rule is what would let the Method part be
    drafted as a results recap, so each part is routed on the sections it
    actually covers. Falls back to the chapter route when the part covers no
    typed section.
    """
    from .chapter_type import route_for_type
    section_types = route.get("section_types") or {}
    families = {route_for_type(section_types[n])[0]
                for n in part.get("numbers", []) if n in section_types}
    if not families:
        return route.get("family", MIXED)
    if len(families) == 1:
        return families.pop()
    return MIXED


def format_stage1_parts(parts: list[dict]) -> str:
    blocks = []
    for part in parts:
        lower = max(300, part["target_words"] - 100)
        upper = part["target_words"] + 150
        blocks.append(
            f"Part {part['index']} -> {part['output']}\n"
            f"Target length: {lower}-{upper} words\n"
            f"Covers: " + "; ".join(part["titles"])
        )
    return "\n\n".join(blocks)


def concatenate_stage1_parts(folder_path: str) -> None:
    folder = Path(folder_path)
    chunks = []
    for index in range(1, 4):
        part_path = folder / f"draft-v1.part-{index}.md"
        if part_path.exists():
            chunks.append(part_path.read_text(encoding="utf-8").strip())
    (folder / "draft-v1.md").write_text("\n\n".join(chunk for chunk in chunks if chunk) + "\n", encoding="utf-8")


def tail_text(path: str, max_chars: int) -> str:
    """Return the trailing max_chars of a file (used to pass the prior part's
    ending into the next part's draft prompt). Empty string if absent."""
    if not os.path.exists(path):
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return "…" + text[-max_chars:]


def extract_json_block(text: str):
    """Parse a JSON object out of possibly-noisy text.

    Tries a ```json fenced block first, then the outermost {...}. Returns the
    parsed object or None. Adapted from AI-Scientist's extract_json_between_markers.
    """
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            cleaned = re.sub(r"[\x00-\x1f]", " ", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                continue
    return None


def read_json_artifact(folder_path: str, name: str):
    """Read and parse a JSON artifact written by an agent. Returns None if the
    file is missing or unparseable (caller decides how to degrade)."""
    path = os.path.join(folder_path, name)
    if not os.path.exists(path):
        return None
    return extract_json_block(Path(path).read_text(encoding="utf-8", errors="replace"))


def _promote_draft(folder_path: str, round_draft: str) -> None:
    """Copy the accepted round's draft to draft-v2.md (the canonical Stage 3 output)."""
    src = os.path.join(folder_path, round_draft)
    if os.path.exists(src):
        content = Path(src).read_text(encoding="utf-8", errors="replace")
        Path(os.path.join(folder_path, "draft-v2.md")).write_text(content, encoding="utf-8")
        print(f"[Manager  ] write_file | draft-v2.md (from {round_draft})", flush=True)


def _unresolved_items(verdict) -> list:
    """Extract the still-unresolved checklist items from a verify verdict."""
    if not isinstance(verdict, dict):
        return []
    items = verdict.get("items", [])
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and not it.get("resolved")]


def _checklist_verdict(frozen: list, verdict) -> tuple:
    """Strictly verify a VERIFY verdict against the frozen acceptance checklist.

    Returns (all_resolved, missing, unknown, dupes):
      - all_resolved: True only if every frozen id appears exactly once, resolved.
      - missing: frozen ids the verdict did not report at all.
      - unknown: ids the verdict reported that are not in the frozen checklist.
      - dupes: ids reported more than once.

    This makes acceptance immune to a hollow ``{"all_resolved": true, "items": []}``:
    acceptance is derived from the checklist, not trusted from a top-level boolean.
    """
    frozen_ids = [it.get("id") for it in frozen if isinstance(it, dict)]
    frozen_set = set(frozen_ids)
    if not isinstance(verdict, dict):
        return False, frozen_ids, [], []
    items = verdict.get("items", [])
    if not isinstance(items, list):
        return False, frozen_ids, [], []
    reported_ids = [it.get("id") for it in items if isinstance(it, dict)]
    reported_set = set(reported_ids)
    missing = [fid for fid in frozen_ids if fid not in reported_set]
    unknown = [rid for rid in reported_set if rid not in frozen_set]
    dupes = [rid for rid in reported_set if reported_ids.count(rid) > 1]
    # Resolved iff every frozen id is present exactly once and marked resolved.
    unresolved_reported = [it for it in items
                           if isinstance(it, dict) and not it.get("resolved")]
    all_resolved = (not missing and not unknown and not dupes and not unresolved_reported)
    return all_resolved, missing, unknown, dupes


def _escalate_unresolved(folder_path: str, rounds: int) -> None:
    """At the round cap, append any unresolved checklist items to todo.md and
    decision.md so they surface for human attention instead of looping forever."""
    verdict = read_json_artifact(folder_path, f"review-verify.round-{rounds}.json")
    unresolved = _unresolved_items(verdict)
    if not unresolved:
        return
    lines = [f"\n## Unresolved after {rounds} revision rounds (needs human attention)"]
    for it in unresolved:
        lines.append(f"- [{it.get('id', '?')}] {it.get('location', '')}: {it.get('note') or it.get('issue', '')}")
    block = "\n".join(lines) + "\n"
    for fname in ("todo.md", "decision.md"):
        path = Path(os.path.join(folder_path, fname))
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        path.write_text(existing + block, encoding="utf-8")


def run_convergence_loop(draft_agent, review_agent, folder_path, folder_rel,
                         reference_excerpt, cross, structure,
                         run_agent_stage, verify):
    """Freeze the first review's MUST FIX list as an acceptance checklist, then
    iterate revise -> verify up to MAX_REVISION_ROUNDS. Each verify round re-checks
    ONLY the frozen checklist (no new issues). Stop when every frozen item is
    resolved; at the round cap, promote the last round and escalate leftovers.

    run_agent_stage and verify are passed in from run_4stage_with_progress (they
    carry that call's closure state). Every round's artifacts are round-numbered so
    a crashed run resumes instead of restarting. Returns the last draft result."""
    review = read_json_artifact(folder_path, "review-v1.json")
    frozen = review.get("must_fix", []) if isinstance(review, dict) else None

    # Fallback: unparseable review or missing must_fix → single revise pass (old behaviour).
    if not isinstance(frozen, list):
        print("[Manager  ] notice     | review-v1.json unparseable; single revise pass", flush=True)
        set_agent_context("Draft")
        result = run_agent_stage(draft_agent, "Draft", (
            f"Read brief.md, input.md, draft-v1.md, review-v1.md from '{folder_path}'. "
            f"Also read '{cross}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
            f"Address ALL 'MUST FIX' items. Write '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. "
            f"If major rewrite, also write '{folder_path}/decision.md'.\n\n{reference_excerpt}"
        ))
        set_agent_context("Manager")
        return result

    if not frozen:
        print("[Manager  ] converge   | no MUST FIX items; one polish pass for SHOULD FIX", flush=True)
        set_agent_context("Draft")
        result = run_agent_stage(draft_agent, "Draft", (
            f"Read brief.md, input.md, draft-v1.md, review-v1.md from '{folder_path}'. "
            f"Also read '{cross}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
            f"There are no MUST FIX items. Address SHOULD FIX items where supported by evidence, "
            f"then write a clean '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'.\n\n{reference_excerpt}"
        ))
        set_agent_context("Manager")
        return result

    print(f"[Manager  ] converge   | frozen acceptance checklist: {len(frozen)} MUST FIX item(s)", flush=True)
    checklist_text = json.dumps(frozen, ensure_ascii=False, indent=2)
    last_result = None

    for round_no in range(1, MAX_REVISION_ROUNDS + 1):
        draft_round = f"draft-v2.round-{round_no}.md"
        verdict_round = f"review-verify.round-{round_no}.json"
        prev_draft = "draft-v1.md" if round_no == 1 else f"draft-v2.round-{round_no - 1}.md"

        # -- revise against the frozen checklist --
        print(f"\n[Manager  ] Round {round_no}/{MAX_REVISION_ROUNDS} | revise → {folder_rel}/{draft_round}", flush=True)
        if not os.path.exists(os.path.join(folder_path, draft_round)):
            set_agent_context("Draft")
            last_result = run_agent_stage(draft_agent, "Draft", (
                f"Read brief.md, input.md, {prev_draft}, review-v1.md from '{folder_path}'. "
                f"Also read '{cross}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below.\n\n"
                f"Resolve EVERY item in this frozen acceptance checklist (JSON):\n{checklist_text}\n\n"
                f"Make the concrete change at each item's location. Preserve everything already correct — "
                f"do not rewrite unaffected paragraphs. Write the full revised draft to '{folder_path}/{draft_round}'.\n\n"
                f"{reference_excerpt}"
            ))
            set_agent_context("Manager")
            if not verify([draft_round]):
                return last_result

        # -- verify ONLY the frozen checklist --
        print(f"[Manager  ] Round {round_no}/{MAX_REVISION_ROUNDS} | verify → {folder_rel}/{verdict_round}", flush=True)
        if not os.path.exists(os.path.join(folder_path, verdict_round)):
            set_agent_context("Review")
            run_agent_stage(review_agent, "Review", (
                f"Read {draft_round} and brief.md from '{folder_path}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below.\n\n"
                f"VERIFY mode: check ONLY whether each item in this frozen checklist is now resolved in {draft_round}. "
                f"Do NOT raise new issues.\n{checklist_text}\n\n"
                f"Write '{folder_path}/{verdict_round}' with EXACTLY this schema (valid JSON only):\n"
                f"{VERIFY_JSON_SCHEMA_HINT}\n"
                f"Set 'all_resolved' true only if every checklist item is resolved.\n\n"
                f"{reference_excerpt}"
            ))
            set_agent_context("Manager")
            if not verify([verdict_round]):
                return last_result

        verdict = read_json_artifact(folder_path, verdict_round)
        all_resolved, missing, unknown, dupes = _checklist_verdict(frozen, verdict)

        if all_resolved:
            print(f"[Manager  ] converge   | all {len(frozen)} item(s) resolved in round {round_no}", flush=True)
            _promote_draft(folder_path, draft_round)
            return last_result

        unresolved = _unresolved_items(verdict)
        n_open = len(unresolved)
        if missing:
            n_open = max(n_open, len(missing))
            print(f"[Manager  ] converge   | round {round_no}: {len(missing)} checklist item(s) not verified ({', '.join(str(x) for x in missing[:5])})", flush=True)
        if unknown:
            print(f"[Manager  ] converge   | round {round_no}: verdict reported unknown id(s): {', '.join(str(x) for x in unknown[:5])}", flush=True)
        if dupes:
            print(f"[Manager  ] converge   | round {round_no}: verdict duplicated id(s): {', '.join(str(x) for x in dupes[:5])}", flush=True)
        print(f"[Manager  ] converge   | round {round_no}: {n_open or '?'} item(s) still unresolved", flush=True)

    # Round cap reached with items still open → promote last round, escalate leftovers.
    _promote_draft(folder_path, f"draft-v2.round-{MAX_REVISION_ROUNDS}.md")
    _escalate_unresolved(folder_path, MAX_REVISION_ROUNDS)
    print("[Manager  ] converge   | round cap reached; unresolved items escalated to todo.md/decision.md", flush=True)
    return last_result


ROUTING_CLAUSES = {
    IDEA: (
        "CHAPTER TYPE: {type} — an IDEA chapter. Its subject is the contribution "
        "itself: what is new, why it works, how it is built. Write it from the "
        "'## Core idea' block of the context pack, which is the author's own "
        "statement of the novelty. The results table is present only as supporting "
        "evidence — quote at most a headline number where it motivates the idea, "
        "and leave detailed reporting to the results chapter. Never substitute "
        "experimental outcomes for an explanation of the mechanism, and never "
        "invent a contribution the author did not claim: write [IDEA NEEDED] or "
        "[DESIGN DETAIL NEEDED] instead."
    ),
    DATA: (
        "CHAPTER TYPE: {type} — a DATA chapter. Its subject is what the experiments "
        "showed. Every number must appear verbatim in the results table of the "
        "context pack; write [MISSING DATA] for anything absent. Use the '## Core "
        "idea' block only to decide which comparisons matter and how to narrate "
        "them — do not re-explain the method design here, it belongs to the method "
        "chapter."
    ),
    MIXED: (
        "CHAPTER TYPE: {type} — a MIXED chapter: it must connect the contribution "
        "to the evidence. Draw the claim from the '## Core idea' block and support "
        "it with numbers from the results table, every one of which must appear "
        "there verbatim ([MISSING DATA] otherwise). Do not introduce a new "
        "contribution here, and do not restate the full method design."
    ),
}


def build_routing_clause(route: dict, family: str = "", part: dict = None) -> str:
    """One paragraph telling the drafter which evidence source is primary.

    The context pack already orders and labels the evidence; this states the rule
    in the prompt too, because the failure mode being prevented (a Method chapter
    written as a results recap) is exactly the kind of drift a model falls into
    when the pack merely *contains* both sources.

    `family` overrides the chapter-level family (used for per-part routing);
    `part` narrows the "spans several types" note to that part's own sections.
    """
    family = family or route.get("family", MIXED)
    label = route.get("type", "unknown")
    section_types = route.get("section_types") or {}
    scope = ([n for n in part.get("numbers", []) if n in section_types]
             if part else sorted(section_types))
    if part and scope:
        # Name the part's own sections, not the whole paper's.
        label = "/".join(dict.fromkeys(section_types[n] for n in scope))
    clause = ROUTING_CLAUSES.get(family, ROUTING_CLAUSES[MIXED]).format(type=label)
    if len({section_types[n] for n in scope}) > 1:
        per_section = ", ".join(f"section {n} is {section_types[n]}" for n in scope)
        clause += (f" This covers several section types ({per_section}); apply the "
                   f"rule above per section rather than uniformly.")
    return clause + "\n\n"


REVIEW_ROUTING_CLAUSES = {
    IDEA: (
        "This is a {type} chapter — an IDEA chapter, judged on whether the "
        "contribution is explained, not on how much data it reports. Raise MUST FIX "
        "when the mechanism is asserted rather than explained, when a design choice "
        "is unmotivated, or when the chapter drifts into reporting results instead "
        "of arguing the idea. Do NOT demand more experimental numbers here, and do "
        "NOT treat an absent metric as a defect — the results chapter carries that. "
        "A [DESIGN DETAIL NEEDED] marker is the correct behaviour when the author's "
        "idea document is silent; flag it for the author instead of asking the "
        "drafter to invent the detail."
    ),
    DATA: (
        "This is a {type} chapter — a DATA chapter, judged on whether the reported "
        "evidence is complete, fairly compared, and traceable to the results store. "
        "Raise MUST FIX for any number not present in the context pack's results "
        "table, missing baselines or ablations, absent variance/seed information, "
        "and claims stronger than the data supports. Do not ask for more exposition "
        "of the method design here."
    ),
    MIXED: (
        "This is a {type} chapter — it must connect the contribution to the "
        "evidence. Raise MUST FIX when a claim about the idea is unsupported by the "
        "results, when a number does not appear in the context pack's results table, "
        "or when the chapter introduces a contribution the author never claimed."
    ),
}


def build_review_routing_clause(route: dict) -> str:
    """Tell the reviewer what standard applies to this chapter type.

    Without it the reviewer applies experiment-paper criteria uniformly and fills
    a Method chapter's MUST FIX list with demands for statistics it was never
    supposed to report — which the convergence loop then treats as a frozen
    acceptance checklist and burns rounds trying to satisfy.
    """
    if PAPER_MODE != "experiment":
        return ""
    family = route.get("family", MIXED)
    clause = REVIEW_ROUTING_CLAUSES.get(family, REVIEW_ROUTING_CLAUSES[MIXED]).format(
        type=route.get("type", "unknown"))
    if route.get("gate") == ADVISORY:
        clause += (" The number gate is ADVISORY for this chapter: an empty or partial "
                   "results store is not itself a defect.")
    elif route.get("gate") == OFF:
        clause += (" The number gate is OFF for this chapter: it is prose, and reporting "
                   "no metrics is correct.")
    return clause + "\n\n"


def build_4stage_manager_prompt(folder_path: str) -> str:
    folder_path = folder_path.replace("\\", "/")
    chapter = chapter_name_from_folder(folder_path)
    reference_excerpt = build_reference_excerpt(chapter)
    return f"""\
Execute a 4-stage paper writing iteration for folder: {folder_path}

Before starting, note the cross-chapter context files:
- paper/00 Background & Example/cross-chapter-state.md (read for terminology and prior chapter summaries)
- paper/01 Structure/final.md (read for the paper's logic line)
- filtered reference excerpt below (use for primary and cross-chapter literature reuse)
- Do not read paper/references/index.md directly; call search_references(query, chapter) only for bounded lookup.

Terminal output rules:
- Print concise progress only, with an explicit owner prefix and relative paths: `[Manager] Stage 1/4 | Calling draft_agent`, `[Manager] Tool      | read_file -> paper/...`, `[Manager] Tool      | write_file -> paper/...`, and `[Manager] Verify    | draft-v1.md exists`.
- Do not print draft prose, review prose, full note contents, full file contents, or long generated markdown to the terminal.
- Let sub-agents write content to files; summarize only file names and status in terminal output.

Stage 1 - Draft:
  Call draft_agent with task: "Read brief.md, input.md, and cross-chapter context from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below to identify primary and cross-chapter papers, then open listed Note Path files only when needed. Do not read paper/references/index.md directly. Write a complete academic draft to '{folder_path}/draft-v1.md' and '{folder_path}/todo.md'."

Stage 2 - Review:
  Call review_agent with task: "Read draft-v1.md, brief.md, and input.md from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below to check whether important primary or cross-chapter references were missed. Do not read paper/references/index.md directly. Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md'. Categorize issues as MUST FIX / SHOULD FIX / CONSIDER."

Stage 3 - Revise:
  Call draft_agent with task: "Read brief.md, input.md, draft-v1.md, and review-v1.md from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below. Do not read paper/references/index.md directly. Address ALL 'MUST FIX' items, including missing reference reuse when supported by notes. Write improved draft to '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. If major rewrite, also write '{folder_path}/decision.md'."

Stage 4 - Finalize:
  Call review_agent with task: "Read draft-v1.md, draft-v2.md, review-v1.md, brief.md, and input.md from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below. Do not read paper/references/index.md directly. Merge the best parts of both drafts, resolving all issues and preserving valid cross-chapter reference reuse. Write '{folder_path}/final.md' as the English publication-ready version, '{folder_path}/final.zh.md' as a Chinese reading/review version for the user, '{folder_path}/decision.md' (explaining what was kept/dropped), and '{folder_path}/todo.md' (remaining issues)."

After each stage, call list_folder on '{folder_path}' to verify the output files exist.
After Stage 4 completes, read '{folder_path}/final.md' and '{folder_path}/todo.md', then update 'paper/00 Background & Example/cross-chapter-state.md' with new terminology decisions, a one-sentence chapter summary, and any unresolved cross-chapter issues.
Report completion status for each stage.

Filtered reference excerpt for this chapter:

{reference_excerpt}
"""


def run_4stage_via_manager(manager_agent, folder_path: str) -> str:
    """Execute 4-stage iteration via Manager Agent (CodeAgent orchestration).

    The manager generates Python code to call draft_agent and review_agent
    in sequence, verifying outputs at each stage.
    """
    return manager_agent.run(build_4stage_manager_prompt(folder_path))


def run_4stage_via_manager_stream(manager_agent, folder_path: str, raw: bool = False) -> list:
    events = []
    seen_progress = set()
    for event in manager_agent.run(build_4stage_manager_prompt(folder_path), stream=True):
        content = getattr(event, "content", None)
        if raw:
            print(content if content is not None else event, end="" if content is not None else "\n", flush=True)
        elif content is not None:
            print_stream_progress(content, seen_progress, include_stage_hints=True)
        else:
            text = str(event)
            if ERROR_RE.search(text):
                print(f"[Manager] Notice    | {sanitize_progress_text(text[:220])}", flush=True)
        events.append(event)
    return events


def run_4stage_direct(draft_agent, review_agent, folder_path: str) -> dict:
    """Execute 4-stage iteration via direct Python orchestration.

    Calls sub-agents sequentially without the Manager Agent.
    More predictable but less flexible.
    Note: cross-chapter-state.md update must be done manually after this completes.
    """
    folder_path = folder_path.replace("\\", "/")
    results = {}

    cross_chapter = "paper/00 Background & Example/cross-chapter-state.md"
    structure = "paper/01 Structure/final.md"
    chapter = chapter_name_from_folder(folder_path)
    reference_excerpt = build_reference_excerpt(chapter)

    print(f"\n[Stage 1/4] Drafting v1...")
    results["stage1_draft"] = draft_agent.run(
        f"Read brief.md and input.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
        f"Write a complete academic draft to '{folder_path}/draft-v1.md' "
        f"and '{folder_path}/todo.md' listing known gaps.\n\n"
        f"{reference_excerpt}"
    )

    if not os.path.exists(os.path.join(folder_path, "draft-v1.md")):
        print("[ERROR] draft-v1.md was not created. Aborting.")
        return results

    print(f"\n[Stage 2/4] Reviewing v1...")
    results["stage2_review"] = review_agent.run(
        f"Read draft-v1.md and brief.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below for terminology consistency and missing reference reuse. "
        f"Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md'. "
        f"Categorize issues as MUST FIX / SHOULD FIX / CONSIDER.\n\n"
        f"{reference_excerpt}"
    )

    if not os.path.exists(os.path.join(folder_path, "review-v1.md")):
        print("[ERROR] review-v1.md was not created. Aborting.")
        return results

    print(f"\n[Stage 3/4] Revising (draft v2)...")
    results["stage3_revise"] = draft_agent.run(
        f"Read brief.md, input.md, draft-v1.md, and review-v1.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
        f"Address ALL 'MUST FIX' items from the review. "
        f"Write improved draft to '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. "
        f"If this is a major rewrite, also write '{folder_path}/decision.md' explaining trade-offs.\n\n"
        f"{reference_excerpt}"
    )

    if not os.path.exists(os.path.join(folder_path, "draft-v2.md")):
        print("[ERROR] draft-v2.md was not created. Aborting.")
        return results

    print(f"\n[Stage 4/4] Finalizing...")
    results["stage4_final"] = review_agent.run(
        f"Read draft-v1.md, draft-v2.md, review-v1.md, brief.md, and input.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
        f"Merge the best parts of both drafts, resolving all issues. "
        f"Write '{folder_path}/final.md' as the English publication-ready version, "
        f"'{folder_path}/final.zh.md' as a Chinese reading/review version for the user, "
        f"'{folder_path}/decision.md' (what was kept/dropped and why), "
        f"and '{folder_path}/todo.md' (remaining issues).\n\n"
        f"{reference_excerpt}"
    )

    if os.path.exists(os.path.join(folder_path, "final.md")):
        print("\n[DONE] All 4 stages completed successfully.")
        print("[NOTE] Remember to update cross-chapter-state.md manually or re-run with Manager mode.")
    else:
        print("\n[WARNING] final.md was not created.")

    return results


def run_4stage_with_progress(draft_agent, review_agent, folder_path: str, manager_agent=None) -> dict:
    """4-stage run with fully Python-controlled, human-readable progress output.

    All smolagents raw streaming is disabled; every visible line is written by this
    function or the shared tool wrappers (read_file / write_file / list_folder).
    """
    folder_path = folder_path.replace("\\", "/")
    folder_rel = display_path(folder_path)
    results = {}

    chapter = chapter_name_from_folder(folder_path)
    # C6: in experiment mode do not inject the survey reference index — the
    # experiment's context source is the results store, not literature rows.
    # The bibliography is still available via search_references / the reader tool.
    reference_excerpt = ("" if PAPER_MODE == "experiment"
                         else build_reference_excerpt(chapter))

    # Chapter-type routing. Parsed up front (before the context pack) because the
    # declared type decides what the pack leads with and whether the number gate
    # may block. An idea chapter drafted from the results store is the
    # architecture-level failure this routing exists to prevent.
    sections = parse_brief_sections(folder_path)
    route = resolve_run_route(folder_path, sections) if PAPER_MODE == "experiment" else {
        "type": "survey", "family": MIXED, "gate": OFF, "source": "default"}
    family, gate = route["family"], route["gate"]

    # Mode-aware context sources. Survey mode reads the cross-chapter files; the
    # standalone experiment mode has neither, so we synthesize a context pack from
    # the idea document and/or results store (content_source) and point the drafts
    # at it instead. Both paths end up as plain file paths the agents read, so
    # downstream code is unchanged. Survey behaviour is byte-identical when
    # PAPER_MODE == "survey".
    if PAPER_MODE == "experiment":
        print(f"[Manager  ] route      | {route_banner(route)}", flush=True)
        if route.get("unrecognized"):
            print(f"[Manager  ] notice     | brief.md declares unknown type "
                  f"'{route['unrecognized']}'; routing as mixed/advisory. "
                  f"Use one of: abstract, intro, related, background, method, theory, "
                  f"experiments, results, analysis, ablation, discussion, limitations, conclusion.",
                  flush=True)
        elif route.get("source") != "brief":
            print(f"[Manager  ] notice     | brief.md declares no 'type:'; inferred from "
                  f"{route.get('source')}. Add a 'type:' line to brief.md to make this explicit.",
                  flush=True)
        # Pre-flight: both primary sources are checked BEFORE any model call.
        # A chapter missing its primary source cannot be written, and discovering
        # that after Stage 0 + Stage 1 wastes four model calls on prose that the
        # gate will reject anyway.
        #
        # An idea-family chapter with no idea document has no primary source: the
        # contribution is the author's to state, not ours to invent.
        if family in (IDEA, MIXED):
            idea_missing = not Path(IDEA_PATH).is_file()
            idea_skeleton, idea_words = False, 0
            if not idea_missing:
                try:
                    from .content_source import load_idea_document, idea_is_skeleton
                    idea_skeleton, idea_words = idea_is_skeleton(load_idea_document())
                except Exception as exc:
                    print(f"[Manager  ] notice     | idea document check skipped: "
                          f"{str(exc)[:160]}", flush=True)
            if idea_missing or idea_skeleton:
                reason = (f"no idea document at {IDEA_PATH}" if idea_missing else
                          f"the idea document at {IDEA_PATH} is still the unfilled template "
                          f"({idea_words} words of authored text)")
                print(f"[Manager  ] FAIL      | idea-family chapter ({route['type']}) but "
                      f"{reason}", flush=True)
                print(f"[Manager  ] FAIL      | the novelty/method design is the primary source for "
                      f"this chapter and cannot be inferred from results.", flush=True)
                action = ("write" if idea_missing else "fill in")
                print(f"[Manager  ] FAIL      | {action} {Path(IDEA_PATH).name} "
                      f"(sections 3 and 4 matter most), then re-run.", flush=True)
                return results
        # Symmetrically: a blocking-gate chapter with no results store has nothing
        # to report. The post-draft number gate would catch this, but only after
        # paying for evidence mining, planning, and three draft parts.
        if gate == BLOCKING:
            try:
                from .number_gate import load_results_store
                if not load_results_store(str(DATA_ROOT)):
                    print(f"[Manager  ] FAIL      | data-family chapter ({route['type']}) but no "
                          f"experiment results under {DATA_ROOT}", flush=True)
                    print(f"[Manager  ] FAIL      | this chapter's content IS the numbers; there is "
                          f"nothing to report and every number would be unverifiable.", flush=True)
                    print(f"[Manager  ] FAIL      | provide data/ results (see data/README.md), "
                          f"then re-run. To draft the idea chapters first, run those instead.",
                          flush=True)
                    return results
            except Exception as exc:
                print(f"[Manager  ] notice     | pre-flight results check skipped: "
                      f"{str(exc)[:160]}", flush=True)
        try:
            from .content_source import build_context_pack, content_source_summary
            context_pack = build_context_pack(chapter, str(DATA_ROOT), family=family)
            pack_path = os.path.join(folder_path, "context-pack.md")
            Path(pack_path).write_text(context_pack, encoding="utf-8")
            print(f"[Manager  ] content    | {content_source_summary(family)}", flush=True)
            print(f"[Manager  ] write_file | {folder_rel}/context-pack.md", flush=True)
        except Exception as exc:
            print(f"[Manager  ] notice     | content pack unavailable: {str(exc)[:160]}", flush=True)
            pack_path = "context-pack.md"
        cross = f"{folder_path}/context-pack.md"
        structure = f"{folder_path}/brief.md"
        # 逐章模式:如果 workspace/cross-chapter-state.md 存在(前章 Manager 写的),
        # 提示词会要求 agent 读取它获取前章术语/结论,保证章节衔接。
        xchap = os.path.join(str(Path(folder_path).parent), "cross-chapter-state.md")
        cross_xchap_hint = (f"\nAlso read '{xchap}' if it exists — it holds prior chapters' "
                            "terminology decisions and conclusions for cross-chapter consistency.\n"
                            if os.path.exists(xchap) else "")
        routing_clause = build_routing_clause(route)
    else:
        cross = "paper/00 Background & Example/cross-chapter-state.md"
        structure = "paper/01 Structure/final.md"
        cross_xchap_hint = ""
        routing_clause = ""

    def verify(expected: list[str]) -> bool:
        for name in expected:
            full = os.path.join(folder_path, name)
            if os.path.exists(full):
                print(f"[Manager  ] verify     | {name} exists", flush=True)
            else:
                print(f"[Manager  ] MISSING    | {name} — stage may have failed", flush=True)
                return False
        return True

    def all_exist(expected: list[str]) -> bool:
        return all(os.path.exists(os.path.join(folder_path, name)) for name in expected)

    def run_agent_stage(agent, agent_name: str, prompt: str):
        attempts = 4
        last_error = None
        for attempt in range(1, attempts + 1):
            print(f"[{agent_name:<8.8}] running    | model request in progress (attempt {attempt}/{attempts})", flush=True)
            try:
                seen_progress = set()
                events = []
                visible_stdout = os.sys.stdout
                filtered_stdout = ProgressFilteringStdout(agent_name, seen_progress, visible_stdout)
                with contextlib.redirect_stdout(filtered_stdout):
                    try:
                        stream = agent.run(prompt, stream=True)
                    except TypeError:
                        return agent.run(prompt)
                    for event in stream:
                        events.append(event)
                        content = getattr(event, "content", None)
                        if content is not None:
                            print_stream_progress(content, seen_progress, agent_name, visible_stdout)
                return events[-1] if events else None
            except Exception as exc:
                last_error = exc
                error_text = " ".join(str(exc).split())[:260]
                if attempt >= attempts:
                    print(f"[{agent_name:<8.8}] failed     | {type(exc).__name__}: {error_text}", flush=True)
                    return {"error": error_text, "exception_type": type(exc).__name__}
                if TRANSIENT_ERROR_RE.search(str(exc)):
                    print(f"[{agent_name:<8.8}] retry      | transient API/network error: {error_text}", flush=True)
                else:
                    print(f"[{agent_name:<8.8}] retry      | {type(exc).__name__}: {error_text}", flush=True)
        return {"error": str(last_error)[:260] if last_error else "unknown error"}

    # ── Stage 0: Evidence mining (pre-draft, multi-perspective QA) ──
    # STORM's perspective-guided QA: interrogate the content source before
    # drafting so the draft is written against grounded evidence, not an outline.
    # Experiment mode only; resumable (skipped if evidence-pack.md exists).
    if PAPER_MODE == "experiment":
        try:
            from .evidence_mining import run_evidence_mining
            pack_file = os.path.join(folder_path, "context-pack.md")
            pack_text = Path(pack_file).read_text(encoding="utf-8", errors="replace") if os.path.exists(pack_file) else ""
            print(f"\n[Manager  ] Stage 0/4  | Evidence mining ({family} perspectives) "
                  f"→ {folder_rel}/evidence-pack.md", flush=True)
            results["stage0_evidence"] = run_evidence_mining(
                draft_agent, folder_path, chapter, pack_text,
                reference_excerpt, run_agent_stage, verify, set_agent_context,
                family=family,
            )
        except Exception as exc:
            print(f"[Manager  ] notice     | evidence mining skipped: {str(exc)[:160]}", flush=True)

    # ── Stage 1: Draft ──────────────────────────────────────────────
    # `sections` was parsed above for chapter-type routing; reuse it.
    stage1_parts = build_stage1_parts(sections)
    stage1_plan = format_stage1_parts(stage1_parts)
    framework = (f"{folder_path}/context-pack.md" if PAPER_MODE == "experiment"
                 else "paper/00 Background & Example/AI大模型综述论文完整框架.md")

    print(f"\n[Manager  ] Stage 1/4  | Draft plan → {folder_rel}/draft-v1.plan.md", flush=True)
    if os.path.exists(os.path.join(folder_path, "draft-v1.plan.md")):
        print("[Manager  ] skip       | draft-v1.plan.md exists", flush=True)
        results["stage1_plan"] = "skipped"
    else:
        planner = manager_agent or draft_agent
        planner_name = "Manager" if manager_agent is not None else "Draft"
        set_agent_context(planner_name)
        evidence_clause = ""
        if PAPER_MODE == "experiment" and os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
            subject = {IDEA: "the idea and its mechanism", DATA: "the results"}.get(
                family, "the idea and the results")
            evidence_clause = (
                f"Also read '{folder_path}/evidence-pack.md': it contains the hard questions "
                f"each perspective posed about {subject} and the grounded answers. Use its "
                f"grounded answers to pick target claims; treat its '## Open Gaps' as drafting "
                f"caveats, not to be silently ignored. ")
        results["stage1_plan"] = run_agent_stage(planner, planner_name, (
            f"You are acting only as the planner for Stage 1. Read brief.md and input.md from '{folder_path}'. "
            f"Also read '{cross}', '{structure}', and '{framework}'. "
            f"{routing_clause}"
            f"{evidence_clause}"
            f"{cross_xchap_hint}"
            f"Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
            f"Write a concrete construction plan to '{folder_path}/draft-v1.plan.md' for exactly three draft parts. "
            f"Use these Python-fixed part boundaries; do not move sections across parts:\n\n{stage1_plan}\n\n"
            f"Begin the plan with a '## Notation and Terminology Table' that fixes, for the whole chapter: "
            f"the canonical spelling of each key term (pick ONE variant, e.g. 'fine-tuning' not 'finetuning'); "
            f"every abbreviation with its first-use expansion; and any mathematical symbol with its definition. "
            f"All three parts must obey this table verbatim, so keep it explicit and unambiguous. "
            f"For each part, specify target claims, required REF IDs, forbidden overlap, transition role, and output file. "
            f"Also write '{folder_path}/todo.md' for known planning gaps. Do not write draft prose.\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
        if not verify(["draft-v1.plan.md", "todo.md"]):
            return results

    results["stage1_parts"] = []
    for position, part in enumerate(stage1_parts):
        print(f"\n[Manager  ] Stage 1{chr(96 + part['index'])}/4 | Draft part {part['index']} → {folder_rel}/{part['output']}", flush=True)
        if os.path.exists(os.path.join(folder_path, part["output"])):
            print(f"[Manager  ] skip       | {part['output']} exists", flush=True)
            results["stage1_parts"].append("skipped")
            continue
        prev_context = ""
        if position > 0:
            prev_output = stage1_parts[position - 1]["output"]
            prev_tail = tail_text(os.path.join(folder_path, prev_output), 1200)
            if prev_tail:
                prev_context = (
                    f"PREVIOUS CONTEXT — the ending of Part {position} (already written). "
                    f"Continue seamlessly from it: do not repeat it, do not contradict its definitions, "
                    f"and keep terminology and symbols identical:\n\"\"\"\n{prev_tail}\n\"\"\"\n\n"
                )
        set_agent_context("Draft")
        evidence_draft_clause = ""
        if PAPER_MODE == "experiment" and os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
            evidence_draft_clause = (
                f"If you need grounded details behind a claim, use read_file on "
                f"'{folder_path}/evidence-pack.md' and read ONLY the Q&A for your part's topics — "
                f"do not inline the whole pack. "
            )
        result = run_agent_stage(draft_agent, "Draft", (
            f"Read brief.md, input.md, and draft-v1.plan.md from '{folder_path}'. "
            f"Also read '{cross}' and '{structure}' only if needed for terminology. "
            f"{build_routing_clause(route, part_family(part, route), part) if PAPER_MODE == 'experiment' else ''}"
            f"{evidence_draft_clause}"
            f"Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
            f"Obey the 'Notation and Terminology Table' in draft-v1.plan.md verbatim: one canonical spelling per term, "
            f"fixed abbreviation expansions, fixed symbol definitions. Never redefine a term or symbol fixed by an earlier part. "
            f"Write only Part {part['index']} to '{folder_path}/{part['output']}'. "
            f"Part boundary:\n{format_stage1_parts([part])}\n\n"
            f"{prev_context}"
            f"Follow draft-v1.plan.md for REF IDs, forbidden overlap, and transitions. "
            f"Do not write other parts and do not duplicate material assigned to other parts.\n\n"
            f"{reference_excerpt}"
        ))
        results["stage1_parts"].append(result)
        set_agent_context("Manager")
        if not verify([part["output"]]):
            return results

    concatenate_stage1_parts(folder_path)
    print(f"[Manager  ] write_file | {folder_rel}/draft-v1.md", flush=True)
    if not verify(["draft-v1.md", "todo.md"]):
        return results

    # ── Deterministic number gate (experiment mode) ─────────────────
    # Cross-check every number in draft-v1 against the results store BEFORE the
    # expensive LLM review. Findings go to number-check.md so the reviewer folds
    # them into its MUST FIX list. Two failure modes:
    #   - MISMATCH (draft contradicts a value in the store): non-blocking, folded
    #     into the review as MUST FIX.
    #   - BLOCKING (no results store / no draft): fail closed — an experiment
    #     paper with no data to verify against is unbuildable, so we abort.
    if PAPER_MODE == "experiment" and gate != OFF:
        try:
            from .number_gate import run_number_gate
            passed, messages = run_number_gate(
                os.path.join(folder_path, "draft-v1.md"), str(DATA_ROOT))
            no_store = not passed and any(
                "[NUMBER-GATE] no results found" in m or "cannot verify numbers" in m
                or "every number in the draft is unverifiable" in m for m in messages)
            report = "# Number consistency check (draft-v1 vs results store)\n\n"
            report += f"Gate level: {gate} (chapter type: {route['type']})\n"
            report += f"Status: {'PASS' if passed else 'MISMATCH — treat as MUST FIX'}\n\n"
            report += "\n".join(f"- {m}" for m in messages) + "\n"
            Path(os.path.join(folder_path, "number-check.md")).write_text(report, encoding="utf-8")
            tag = "OK" if passed else "MISMATCH"
            print(f"[Manager  ] number-gate| {gate}: {tag} → {folder_rel}/number-check.md", flush=True)
            if no_store and gate == BLOCKING:
                # Blocking condition: this chapter's content IS the numbers, and
                # there are none. Do not proceed into an LLM review that would
                # argue about unverifiable values.
                print(f"[Manager  ] FAIL      | number gate is BLOCKING for a {route['type']} chapter: "
                      f"no experiment results to verify against", flush=True)
                print(f"[Manager  ] FAIL      | provide data/ results first, then re-run.", flush=True)
                return results
            if no_store and gate == ADVISORY:
                # An idea chapter may legitimately precede the experiments. Say so
                # once and continue — but any number it does write is unverified.
                print(f"[Manager  ] number-gate| advisory: no results store yet; "
                      f"numbers in this {route['type']} chapter are UNVERIFIED", flush=True)
        except Exception as exc:
            print(f"[Manager  ] notice     | number gate skipped: {str(exc)[:160]}", flush=True)
    elif PAPER_MODE == "experiment":
        print(f"[Manager  ] number-gate| off (chapter type: {route['type']} — prose only)", flush=True)

    # ── Stage 2: Review (Markdown + structured JSON checklist) ──────
    print(f"\n[Manager  ] Stage 2/4  | Review → {folder_rel}/review-v1.md (+ review-v1.json)", flush=True)
    if all_exist(["review-v1.md", "review-v1.json"]):
        print("[Manager  ] skip       | review-v1.md/review-v1.json exist", flush=True)
        results["stage2"] = "skipped"
    else:
        set_agent_context("Review")
        number_check_clause = ""
        if PAPER_MODE == "experiment" and os.path.exists(os.path.join(folder_path, "number-check.md")):
            number_check_clause = (
                f"Also read '{folder_path}/number-check.md': it is a deterministic cross-check of "
                f"every number in the draft against the results store. Treat each [NUMBER-MISMATCH] "
                f"line as a MUST FIX item (the prose contradicts the recorded results). "
            )
        results["stage2"] = run_agent_stage(review_agent, "Review", (
            f"Read draft-v1.md, brief.md, input.md from '{folder_path}'. "
            f"Also read '{cross}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
            f"{build_review_routing_clause(route)}"
            f"{number_check_clause}"
            f"Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md', "
            f"categorizing issues as MUST FIX / SHOULD FIX / CONSIDER.\n\n"
            f"THEN write a machine-readable '{folder_path}/review-v1.json' with EXACTLY this schema "
            f"(valid JSON only, no trailing commas, no comments):\n{REVIEW_JSON_SCHEMA_HINT}\n"
            f"The must_fix array must list every MUST FIX issue as its own object with a stable id "
            f"(MF1, MF2, ...), a precise location, the issue, and a concrete suggestion. "
            f"This array becomes the frozen acceptance checklist for revision, so be exhaustive and specific. "
            f"The needs_citation array must list every sentence that makes a factual claim "
            f"deserving a citation but has no \\\\cite. Suggest a key from the user's bibliography "
            f"only when you are confident it matches; otherwise set suggested_key to null (the "
            f"framework will hand it to the user, never invent a citation).\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
    if not verify(["review-v1.md", "review-v1.json"]):
        return results

    # C3: validate review-v1.json BEFORE entering the convergence loop. A review
    # with an unparseable JSON or a missing/empty must_fix would silently degrade
    # to a single unverified revise pass — the worst kind of "passed". Enforce the
    # schema: must_fix must be a list of {id, location, issue, suggestion}. If it
    # is not, delete the artifacts and force Stage 2 to re-run.
    _review_data = read_json_artifact(folder_path, "review-v1.json")
    _review_must_fix = _review_data.get("must_fix") if isinstance(_review_data, dict) else None
    if not isinstance(_review_must_fix, list) or not all(
        isinstance(it, dict) and it.get("id") and it.get("location")
        for it in _review_must_fix
    ):
        print("[Manager  ] notice     | review-v1.json has an invalid must_fix; forcing Stage 2 re-run", flush=True)
        os.remove(os.path.join(folder_path, "review-v1.json"))
        if os.path.exists(os.path.join(folder_path, "review-v1.md")):
            os.remove(os.path.join(folder_path, "review-v1.md"))
        return results  # caller will re-enter; stage2 will run because artifacts removed

    # ── C11: apply citation insertions from the review ──────────────
    # The reviewer flagged needs_citation (sentence + suggested key). A Python
    # pass mechanically inserts \cite{key} at each flagged sentence's end, only
    # for bib-sourced keys from the user's bibliography. Anything without a
    # confident local key is handed to the user (needs_human), never fabricated.
    try:
        from .citation_supplement import apply_insertions
        needs_citation = _review_data.get("needs_citation") if isinstance(_review_data, dict) else None
        insertions = [{"sentence": it.get("sentence", ""),
                       "suggested_key_or_url": it.get("suggested_key") or "",
                       "source": "bib" if it.get("suggested_key") else "none"}
                      for it in (needs_citation or []) if isinstance(it, dict)]
        if insertions:
            draft_path = os.path.join(folder_path, "draft-v1.md")
            if os.path.exists(draft_path):
                text = Path(draft_path).read_text(encoding="utf-8", errors="replace")
                new_text, applied, skipped = apply_insertions(text, insertions)
                if applied:
                    Path(draft_path).write_text(new_text, encoding="utf-8")
                report_lines = [f"- applied \\cite to {applied} sentence(s)",
                                f"- {len(skipped)} left to human:"]
                for s in skipped[:20]:
                    report_lines.append(f"  - {s.get('sentence','')[:80]} ({s.get('reason','')[:40]})")
                Path(os.path.join(folder_path, "citation-insertions.md")).write_text(
                    "\n".join(report_lines) + "\n", encoding="utf-8")
                print(f"[Manager  ] cite-apply | {applied} inserted, {len(skipped)} needs human → {folder_rel}/citation-insertions.md", flush=True)
            else:
                print(f"[Manager  ] notice     | draft-v1.md missing; citation insertions skipped", flush=True)
    except Exception as exc:
        print(f"[Manager  ] notice     | citation insertions skipped: {str(exc)[:160]}", flush=True)

    # ── Stage 3: Revise with convergence loop ───────────────────────
    print(f"\n[Manager  ] Stage 3/4  | Revise (convergence loop, max {MAX_REVISION_ROUNDS} rounds) → {folder_rel}/draft-v2.md", flush=True)
    if os.path.exists(os.path.join(folder_path, "draft-v2.md")):
        print("[Manager  ] skip       | draft-v2.md exists", flush=True)
        results["stage3"] = "skipped"
    else:
        results["stage3"] = run_convergence_loop(
            draft_agent, review_agent, folder_path, folder_rel,
            reference_excerpt, cross, structure,
            run_agent_stage, verify,
        )
    if not verify(["draft-v2.md"]):
        return results

    # ── Stage 4: Finalize ────────────────────────────────────────────
    print(f"\n[Manager  ] Stage 4/4  | Finalize → {folder_rel}/final.md", flush=True)
    final_outputs = ["final.md", "final.zh.md", "decision.md", "todo.md"]
    if all_exist(final_outputs):
        print("[Manager  ] skip       | final.md/final.zh.md/decision.md/todo.md exist", flush=True)
        results["stage4"] = "skipped"
    else:
        set_agent_context("Review")
        results["stage4"] = run_agent_stage(review_agent, "Review", (
            f"Read draft-v1.md, draft-v2.md, review-v1.md, brief.md, input.md from '{folder_path}'. "
            f"Also read '{cross}' and '{structure}'. Do not read paper/references/index.md directly; use the filtered reference excerpt below. "
            f"Merge the best parts, resolve all issues. "
            f"Write '{folder_path}/final.md' (English), '{folder_path}/final.zh.md' (Chinese review), "
            f"'{folder_path}/decision.md', and '{folder_path}/todo.md'.\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
    all_ok = verify(final_outputs)

    # I2: re-run the number gate on the FINAL text. Revise/finalize may have
    # introduced numbers that never appeared in draft-v1, and those must be
    # cross-checked too. If any mismatch, surface it in todo.md — never let a
    # finalized paper silently carry a number that contradicts the results store.
    if PAPER_MODE == "experiment" and all_ok and gate != OFF:
        try:
            from .number_gate import run_number_gate
            final_gate_passed, final_gate_msgs = run_number_gate(
                os.path.join(folder_path, "final.md"), str(DATA_ROOT))
            # Advisory chapters with no store yet: nothing to compare against, so
            # a "no results found" verdict is not a finding worth writing to todo.
            if gate == ADVISORY and not final_gate_passed and any(
                    "no results found" in m or "every number in the draft is unverifiable" in m
                    for m in final_gate_msgs):
                print(f"[Manager  ] number-gate| advisory: no results store; final numbers UNVERIFIED",
                      flush=True)
            elif not final_gate_passed:
                block = "\n## Final number gate: MISMATCHES to fix before submission\n"
                for m in final_gate_msgs:
                    block += f"- {m}\n"
                todo_path = Path(os.path.join(folder_path, "todo.md"))
                existing = todo_path.read_text(encoding="utf-8", errors="replace") if todo_path.exists() else ""
                todo_path.write_text(existing + block, encoding="utf-8")
                print(f"[Manager  ] number-gate| FINAL MISMATCH → appended to {folder_rel}/todo.md", flush=True)
            else:
                print(f"[Manager  ] number-gate| final.md numbers OK", flush=True)
        except Exception as exc:
            print(f"[Manager  ] notice     | final number gate skipped: {str(exc)[:160]}", flush=True)

    if all_ok:
        print(f"\n[Manager  ] DONE       | All 4 stages complete for {folder_rel}", flush=True)
        print("[Manager  ] NOTE       | Update cross-chapter-state.md after reviewing final.md", flush=True)
    return results
