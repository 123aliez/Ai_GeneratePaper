# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A multi-agent framework that writes an **experimental research paper** from three user-supplied inputs. Three heterogeneous agents (Draft / Review / Manager, each with its own model + API key in `.env`) collaborate on the filesystem, per chapter: evidence mining → planning → part-wise drafting → review → convergent revision → finalization → cross-chapter handoff. It is the experiment-paper branch of a `survey/` framework; `PAPER_MODE=survey` keeps a parity path but this repo has no survey content (guarded by `_assert_survey_mode`).

The framework's design principle: **the paper's contribution is the idea, not the numbers**. `inbox/idea.md` states the novelty/mechanism/method; `inbox/results/` supplies experiment numbers as supporting evidence. Anti-hallucination is a hard architectural concern — the framework refuses to fabricate, and fails closed rather than "reasonably" filling gaps.

One paper is one project folder (`python run.py --new <name>`). Author material lives under `inbox/` and is read-only to the agents; generated indexes live under `_work/index/`; chapter artifacts live under `_work/workspace/`; assembled deliverables land in `output/`.

## Project inputs (`inbox/`)

| Path | Owns | If missing |
|---|---|---|
| `inbox/idea.md` | **The contribution** — novelty, core insight, method design | material check errors; idea-family chapters refuse to draft |
| `inbox/outline_draft.md` | **The skeleton** — a table of chapters (title + `type:`). `--expand` turns it into `outline.zh.md` (author reviews) and `_work/outline.md` (what the agents read) | material check errors; `--init` has no input |
| `inbox/results/*.csv` + `实验设置.md` | **The numbers** — one CSV is one result table; the settings file is the only source of setup prose | data-family chapters refuse to draft |
| `inbox/references/` | **The citations** — `refs.bib` plus level folders `A/` `B/` `C/` | no citable keys |
| `inbox/code/` | experiment code, statically indexed, never executed | experiments chapters mark setup details `[UNKNOWN]` |

A filled-but-unedited `idea.md` template is detected by word count (`content_source.idea_is_skeleton`) and treated as missing. `input.md` per chapter holds chapter-local source material (assembled by the Manager at Stage 0a from `_work/index/ref-index.md`) and is **never overwritten**. `_work/index/data-index.md` is a program-built grid (`{{RES-xxx}}=value` per cell) regenerated whenever results change; the number gate still reads the CSVs raw as ground truth.

### Reference levels (`inbox/references/A|B|C`)

`agents/references.py::scan_library` treats only the uppercase folders `A/`, `B/`, `C/` as level folders. A note is either a `.md` dropped directly in the level folder (no fulltext, no warning) or one subfolder (one note, optional fulltext). Any other top-level folder is warned and skipped — it does not enter the index. The level says how deeply a paper is read, never whether a chapter may cite it:

- **A** — full filtered note; fulltext excerpts allowed.
- **B** — the note, but only the part this chapter needs; fulltext excerpts allowed.
- **C** — the one-line summary only. `refs/<key>.md` contains no note body, `ref-fulltext.json` omits the key, and `search_reference_text` refuses it.

Matching is still `doi → arxiv → title` against `refs.bib`. Agents cannot read `inbox/references/` directly (`tools._in_references_room`).

## Common commands

```bash
pip install -r requirements.txt                          # smolagents[openai], anthropic, python-dotenv
cp .env.example .env                                     # fill DRAFT_*/REVIEW_*/MANAGER_* provider/model/key/base

python run.py --new 项目名 [--parent 目录]                 # create a project from templates/
python run.py --project <项目> --check                    # material check only
python run.py --project <项目> --index                    # build the three indexes (references / data / code)
python run.py --project <项目> --expand                   # expand the skeleton → outline.zh.md + _work/outline.md
python run.py --project <项目> --init [--force]           # outline → per-chapter workspaces
python run.py --project <项目> --all                      # run every chapter
python run.py --project <项目> 03-results                 # run one chapter
python run.py --project <项目> --assemble                 # re-verify finals and assemble (no model calls)
python run.py --project <项目> --list                     # per-chapter status
python run.py --project <项目> --go                       # check → expand (stop for review) → chapters → assemble
python run.py --project <项目> --go --keep-outline        # idea/skeleton changed; keep the current outline

# Offline tests — no API calls (this is how wiring changes are validated)
for t in tests/test_*.py; do python "$t" || break; done
```

**In experiment mode (`PAPER_MODE=experiment`, the default), the orchestrated pipeline is the only valid run path.** `--direct`, `--stream-raw`, and the old Manager-driven path are survey-only and explicitly refused (`_assert_survey_mode()`). The old intake and retrieve pre-passes no longer exist: references are the author's own notes under `inbox/references/`.

## Architecture — the parts that require reading multiple files

### One routing dimension, decided in Python, drives everything

**Chapter-type routing** (`agents/chapter_type.py`) — WHAT a chapter is written from. The `type:` line in `brief.md` resolves to `(family, gate)`:
- `family`: `idea` (method/intro/abstract/theory → primary source `idea.md`), `data` (results/experiments/analysis/ablation → primary source `data/`), `mixed` (discussion/limitations/conclusion + unknown fallback).
- `gate`: `blocking` (no results store ⇒ abort before any model call), `advisory` (proceed, numbers marked UNVERIFIED), `off` (pure prose, gate not run).
- Alias map is deliberately generous (Chinese aliases, "ablation study", decorated forms). An unrecognized type degrades to `mixed/advisory` and is *reported*, never silently dropped. Type can also be set per-section (`- type: results` inside a numbered section) and per-drafting-part; `resolve_run_route` unions section types (e.g. a whole-paper brief with idea + data sections → `mixed`/`blocking`).

**There is only one write mode now** — the `SINGLE`/standalone route was removed. Every chapter folder is generated by `--init` from `outline.md` and is chapter *N* of the whole paper: it knows its position, reuses terminology from `cross-chapter-state.md`, and opens/closes with transitions to its neighbors. `resolve_write_mode()` in `agents/outline.py` raises `OutlineRouteError` (not a fallback) if the folder is missing from `outline.md` — an out-of-outline folder is not a legal chapter. The single writing contract (`build_mode_clause` / `build_mode_review_clause`) is injected into **every stage that rewrites prose** (1a, 1b–1c, each Stage-3 round, Stage 4); missing one lets chapter prose drift from the cross-chapter contract while the pipeline still prints success.

`--all` requires `workspace/` to match `outline.md` exactly: a missing folder (never `--init`-ed) or an extra folder (renamed-title leftover) aborts before any chapter runs, so `--all` never runs a subset while reporting full completion. `workspace/` is git-tracked via `.gitkeep` only — all chapter artifacts are gitignored runtime output. `workspace/_EXAMPLE-whole-paper/` is a scaffold (single-brief whole-paper demo with per-section `type:`); `_`-prefixed folders are never treated as chapters.

### Model layer — dual protocol (LiteLLM removed)

OpenAI and Claude no longer share one LiteLLM call layer. `config.py`'s three getters (`get_draft_model` / `get_review_model` / `get_manager_model`) route through `agents/model_router.build_model(provider, model, ...)`:

- `provider=openai` → smolagents `OpenAIModel` (openai SDK, OpenAI-compatible API)
- `provider=anthropic` → `agents/anthropic_model.AnthropicModel` (anthropic SDK, native `/v1/messages`)

`provider` is an explicit `*_PROVIDER` env field — **not** inferred from the model-id prefix (the same Claude may expose both OpenAI- and Anthropic-compatible endpoints). `*_REASONING_LEVEL` (`off|low|medium|high|xhigh|max`) is mapped by `agents/model_capabilities.py` to each provider's native param (OpenAI → `reasoning_effort`; Claude → `output_config.effort` / `thinking`), and **explicitly raises on a level the (provider, model) doesn't support** rather than silently downgrading. The capability table is keyed per model version (e.g. `claude-opus-4-6` lacks `xhigh`). Default timeouts: OpenAI 300s, Claude 600s; `MODEL_MAX_RETRIES=0` in triage. The three getters' signature/return (a smolagents `Model`) is unchanged → downstream (`agents.py`/`orchestrator.py`/`run.py`) is untouched.

### Chapter contracts (`agents/chapter_contract.py`)

Each of the 13 canonical chapter types has a contract: **focus** (what the chapter writes), **references** (how the library may be used), **forbidden** (what must not appear). `CONTRACTS` is the single source; `theory`/`analysis`/`ablation`/`limitations` share their base type's dict object. Unknown types get `DEFAULT_CONTRACT`. Three clause builders derive from it:

- `build_contract_clause(route)` — appended to `mode_clause`, so every prose-rewriting stage (1a, each 1b part, each Stage-3 revision round, Stage 4) carries it via the existing single injection point.
- `build_contract_review_clause(route)` — appended to `mode_review_clause` (Stage 2), each type one paragraph, plus `NOVELTY_GUARD` exactly once. The VERIFY round gets neither — it only re-checks the frozen checklist and must not raise new issues.
- `build_contract_reference_rule(route)` — Stage 0a material prep (`run_input_material(route=...)`), plus the four-column input.md table (`| key | level | section(s) | use in this chapter |`).

`check_contract` / `write_contract_check` are a deterministic lint (method/theory: comparative wording next to a `\cite`; abstract/conclusion: any `\cite`), written to per-chapter `contract-check.md` after draft-v1 concatenation, after the Stage-4 final number gate, and in `reverify_chapter`. Hints only — never a verification reason (D13), never fingerprinted. `assemble.Chapter.contract` surfaces non-`skipped` hints in 待补清单.md as `[章节约束]` rows.

The reviewer is explicitly forbidden from judging novelty (D3/D11): the rubric's Originality row was replaced by Fidelity, and `REVIEW_INSTRUCTIONS`' Prohibited Behaviors end with "Never judge whether the contribution is novel".

Evidence-mining perspectives switch by chapter type (`perspectives_for_route`): a run whose types are all `method`/`theory` uses `METHOD_PERSPECTIVES` (Attribution reviewer first — which base techniques can be credited with one citation — instead of the old Novelty reviewer); everything else stays family-based. The first IDEA perspective is now "Positioning reviewer": differences to prior work come ONLY from idea.md section 5, never derived from notes.

### Short chapters — synthetic single section

`abstract` / `conclusion` / `limitations` / `discussion` (`SHORT_CHAPTER_TYPES` in `agents/outline.py`) are too short (200-600 words) to split into `###` subsections. The author writes bullets directly under the `##` chapter heading; `parse_outline`'s `_maybe_synthesize_short_section` folds those chapter-level bullets into ONE `synthetic: True` section (title = chapter title, `target_words` from `DEFAULT_WORDS_BY_TYPE` or a `## ... (~N words)` chapter-level annotation). This reuses the entire existing section machinery — `render_brief` writes `1. **Abstract** (~200 words)`, `build_stage1_parts` yields a single drafting part, `chapter_fingerprint` stays stable across old↔new-style migration. `render_outline` skips the `###` line for synthetic sections (round-trips the short shape). Synthesis is **short-chapter-only**: a normal chapter (method/results) with chapter-level bullets does NOT synthesize — it must write `###` subsections (else `--expand`'s "2-4 subsections" check would be bypassable). `validate_expansion` forbids adding `###` to a short chapter that had none, and the `--expand` prompt's EXCEPTION tells the Manager to keep short-chapter bullets at chapter level.

### The pipeline (`agents/orchestrator.py`, `run_4stage_with_progress`)

Orchestration is deterministic Python, not Manager-driven. Per chapter:

```
routing      → (resolve_write_mode / resolve_run_route; pre-flight gates)
Stage 0a     → input.md         (Manager, prepared from _work/index/ref-index.md — never overwritten;
                                  picks references by level + chapter contract, four-column table)
Stage 0b     → evidence-pack.md  (STORM-style multi-perspective QA; perspectives switch by family
                                  AND by chapter type — see Chapter contracts)
Stage 1a     → draft-v1.plan.md  (Manager: plan + frozen Notation/Terminology Table)
Stage 1b~    → draft-v1.part-N.md (part count = min(#sections, 3); each part routed on its own sections)
              → draft-v1.md       (concatenated by Python, using the actual part count)
              → number-check.md   (number gate vs results store, strictness by gate level)
              → contract-check.md (deterministic chapter-contract lint; hints only)
Stage 2      → review-v1.md + review-v1.json (MUST FIX frozen checklist; criteria by family + contract check)
              → citation-insertions.md
Stage 3      → draft-v2.md       (convergence loop: revise↔verify frozen checklist, ≤ MAX_REVISION_ROUNDS)
Stage 4      → final.md + final.zh.md + decision.md + todo.md (number gate re-run, OVERWRITES number-check.md;
                                  contract-check recomputed on final.md)
Stage 5      → cross-chapter-state.md  (via cross-chapter-draft.md candidate + atomic replace + Python validation)
```

Key invariants to respect when editing:

- **Every prose-rewriting stage is pointed at `idea.md` directly** via `idea_clause(family)` injected as the first line of the task prompt — the agent reads the full global `idea.md` itself (`read_file`), never a copied snippet. Data/mixed-family stages additionally point at the RES-ID catalog (`_work/index/res-ids.md` in project mode). There is **no `context-pack.md`** anymore; the whole `pack_fingerprint` machinery is gone.
- **RES placeholders (evolution ②)**: when the results store has numeric values, `ensure_res_ids()` generates/merges the RES catalog (stable IDs `RES-001…`, sorted by key; old IDs never renumbered on resume, dead keys' IDs never reused, values always re-read from the store). Drafting stages for data/mixed chapters are told to cite result numbers ONLY as verbatim `{{RES-xxx}}` placeholders; Stage 4 finalization runs `fill_result_refs()` to substitute recorded values into `final.md`/`final.zh.md`, writing unknown IDs as a visible `⟨未知结果 xxx⟩` marker plus a todo.md entry — never a guessed value. `number_gate` strips placeholders before checking (a placeholder's digits are not evidence), so hand-typed wrong values are still caught.
- **Stage fingerprints (evolution ③)**: `agents/skills_registry.py` keeps `SKILL_REGISTRY` (role → whitelisted skill files) and `.stage-manifest.json` accounting per chapter. Every "exists ⇒ skip" artifact is fingerprinted at generation time (`record_stage`) as brief-outline-fingerprint + stage name + prompts.py/skills content hash + model group; before a run, `stale_stage_artifacts` hard-stops (no deletion) on any existing artifact that is unrecorded or fingerprint-mismatched — pre-evolution workspaces are stale by default. To deliberately keep an old artifact, delete its entry from `.stage-manifest.json`. `input.md` is deliberately NOT a stale candidate (may contain author hand-edits; re-running would only skip it anyway) but is still recorded when generated.
- **Structured single-turn review JSON (evolution ①)**: Stage 2's `review-v1.json` is produced by `run_structured_call` (`agents/structured_call.py`) — a direct `model.generate()` call that bypasses the CodeAgent loop, because a JSON-fenced model output gets executed as code inside CodeAgent (the MathModel_Paper lesson). Parse failure returns None and the orchestrator falls back to the CodeAgent path; model exceptions propagate (ResilientModel already retried). Test stubs advertise `_offline_test_stub = True` to skip the real-model path offline.
- **Resume = "file exists ⇒ skip."** Every artifact is skipped if present. Route changes are captured by the `outline-fingerprint` in `brief.md` line 1 (from `chapter_fingerprint`, covering type + per-section types + bullets); a changed outline without re-`--init` hard-stops the run with the stale-artifact list — nothing is ever deleted automatically. Prompt/skill/model changes are captured by the stage fingerprints above.
- **Convergence acceptance is checklist-derived, not trusted from a top-level boolean.** `_checklist_verdict` requires every frozen MUST FIX id to appear exactly once, resolved, with no unknown/duplicate ids — immune to a hollow `{"all_resolved": true, "items": []}`.
- **Brief-source gates hard-stop before any model call**: folder missing from `outline.md` (`resolve_write_mode` raises), a non-generated brief (no outline fingerprint), a brief stale against the current outline, or a missing `cross-chapter-state.md`.
- **Stage 5 writes `cross-chapter-draft.md` first** and validates it (other chapters' entries preserved line-by-line, own `- [<chapter>] ` claim present in the Key Claims section, three `## ` headings intact) before atomic `os.replace`. Failure blocks `--all`.
- **The number gate runs twice and writes the same `number-check.md`** — Stage 2's review folds the first run's mismatches into MUST FIX, then Stage 4 overwrites the file with the final-draft re-check (there is no separate `number-check-final.md`).

### Anti-hallucination components

- `agents/result_ids.py` — RES-ID placeholder system: stable IDs over the flattened results store, merge semantics that never renumber on resume, and finalization-time substitution (`fill_result_refs`) that leaves a visible hole rather than guessing a value. Stdlib only.
- `agents/number_gate.py` — every number in a draft cross-checked against the flattened results store (AI-Scientist-inspired). Prefers a miss over a false alarm: a number is only flagged when adjacent to a metric phrase derived from a result key, compared under percent/fraction scale factors. Both run dir and run file layouts are supported for run identity. Runs after `strip_placeholders` so `{{RES-xxx}}` tokens are not treated as numeric claims.
- `agents/assemble.py` — post-conversion static checks on the LaTeX tree: citation keys must exist in the exported `refs.bib`, labels unique, `\ref` resolvable, environments balanced. The build does not compile; it hands the author a checked工程.
- `agents/citation_supplement.py` — auto-inserts `\cite{key}` only from `refs.bib` keys; anything uncertain goes to `needs_human`. Never fabricates a key.
- `agents/references.py` + `agents/tools.py` — the controlled reference room: agents see only the program-built index, filtered note views, and capped fulltext excerpts (`read_reference` / `search_reference_text`); `inbox/references/` itself is unreadable (`_in_references_room`), and C-level papers expose nothing beyond their one-line summary.

### Content source (`agents/content_source.py`)

No aggregated context pack anymore. This module only reads/tidies raw material: `load_idea_document` / `idea_is_skeleton` (pre-flight gate), and results-store overviews. `data_dir_has_content` decides whether data-index generation is skipped. Textual run metadata in the results store is marked with a `:` prefix to keep it out of the citable-number table.

## Conventions worth knowing

- Codebase comments and user-facing CLI messages are **predominantly Chinese**; prompts are English. Keep both.
- Agent tools are thin wrappers over `read_file`/`read_file_range`/`write_file`/`list_folder`/`read_reference`/`search_reference_text` (`agents/tools.py`); they log to stdout with a `[Owner] action | path` shape that the progress filter parses. `set_agent_context()` switches the owner tag.
- `config.py` is the single source of model config: three independent `*_PROVIDER`/`*_MODEL`/`*_API_KEY`/`*_API_BASE`/`*_REASONING_LEVEL` groups (draft/review/manager), resolved lazily by `get_*_model()` → `agents.model_router.build_model()`. It no longer imports smolagents at top level and no longer pollutes global `OPENAI_API_KEY`/`OPENAI_API_BASE`.
- Skill files in `skills/` are governed by `agents/skills_registry.py`'s `SKILL_REGISTRY` whitelist — the registry decides which files are fingerprinted and (via `build_skill_injection`, currently exercised by tests) deterministically injectable per role. Agents may still self-serve `read_file` them (backwards compatible); only whitelisted content participates in stage fingerprints.
- `__init__.py` re-exports; modules reach project root via `sys.path.insert` rather than relying on package install.
