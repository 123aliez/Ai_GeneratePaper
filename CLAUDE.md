# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A multi-agent framework that writes an **experimental research paper** from three user-supplied inputs. Three heterogeneous agents (Draft / Review / Manager, each with its own model + API key in `.env`) collaborate on the filesystem, per chapter: evidence mining → planning → part-wise drafting → review → convergent revision → finalization → cross-chapter handoff. It is the experiment-paper branch of a `survey/` framework; `PAPER_MODE=survey` keeps a parity path but this repo has no survey content (guarded by `_assert_survey_mode`).

The framework's design principle: **the paper's contribution is the idea, not the numbers**. `idea.md` states the novelty/mechanism/method; `data/` supplies experiment numbers as supporting evidence. Anti-hallucination is a hard architectural concern — the framework refuses to fabricate, and fails closed rather than "reasonably" filling gaps.

## The three inputs (single source of truth each)

| File | Owns | If missing |
|---|---|---|
| `idea.md` (root) | **The contribution** — novelty, core insight, method design | idea-family chapters refuse to draft |
| `outline.md` (root) | **The structure** — chapters (`##`, each with `type:`), sections (`###`, with `(~N words)`) | `--init` has no input; nothing can run |
| `data/` | **The numbers** — results store (CSV/JSON/logs/plots) | data-family chapters refuse to draft |

`idea.md`, `outline.md`, `data/**` are gitignored (templates and contract docs are committed). A filled-but-unedited `idea.md` template is detected by word count (`content_source.idea_is_skeleton`, ~40-word threshold) and treated as missing. `input.md` per chapter holds chapter-local source material (assembled by the Manager at Stage 0 from `references/bibliography.md`) and is **never overwritten** — `--init` no longer generates it. `data/data-index.md` is a Manager-built three-level index (experiment → result → specific value) generated at `--init` time as the navigation layer for data-family chapters; the number gate still reads `data/` raw as ground truth.

## Common commands

```bash
pip install -r requirements.txt     # smolagents[litellm], python-dotenv
cp .env.example .env                # fill DRAFT_*/REVIEW_*/MANAGER_* model/key/base

python run.py --expand             # Manager expands outline skeleton → outline.expanded.md (review it, then mv to outline.md)
python run.py --init               # outline.md → workspace/<NN-chapter>/ + cross-chapter-state.md
python run.py --init --force       # refresh brief.md after editing outline.md (input.md untouched)
python run.py --list               # show each chapter's route + type + status

python run.py --all --progress     # run full paper in outline order; stops on first failure
python run.py 04-method --progress # run one chapter

python latex/build.py --paper NAME # citation gate + LaTeX compile
python latex/build.py --check-only # citation closure only

# Offline tests — no API calls (this is how wiring changes are validated)
python tests/test_routing.py           # type routing, aliases, per-section routing
python tests/test_outline.py           # outline parsing, brief round-trip, chapter position
python tests/test_pipeline_routing.py  # full pipeline with fake agents, gate wiring
python tests/test_optimizations.py     # part sizing, route fingerprint, tool chain
python tests/test_expand.py            # --expand constraints + validate_expansion
```

**In experiment mode (`PAPER_MODE=experiment`, the default), `--progress` is the only valid run path.** `--direct`, `--stream-raw`, and the default Manager-driven path are survey-only and explicitly refused. This is enforced in both `run.py` and `_assert_survey_mode()` in the orchestrator.

## Architecture — the parts that require reading multiple files

### One routing dimension, decided in Python, drives everything

**Chapter-type routing** (`agents/chapter_type.py`) — WHAT a chapter is written from. The `type:` line in `brief.md` resolves to `(family, gate)`:
- `family`: `idea` (method/intro/abstract/theory → primary source `idea.md`), `data` (results/experiments/analysis/ablation → primary source `data/`), `mixed` (discussion/limitations/conclusion + unknown fallback).
- `gate`: `blocking` (no results store ⇒ abort before any model call), `advisory` (proceed, numbers marked UNVERIFIED), `off` (pure prose, gate not run).
- Alias map is deliberately generous (Chinese aliases, "ablation study", decorated forms). An unrecognized type degrades to `mixed/advisory` and is *reported*, never silently dropped. Type can also be set per-section (`- type: results` inside a numbered section) and per-drafting-part; `resolve_run_route` unions section types (e.g. a whole-paper brief with idea + data sections → `mixed`/`blocking`).

**There is only one write mode now** — the `SINGLE`/standalone route was removed. Every chapter folder is generated by `--init` from `outline.md` and is chapter *N* of the whole paper: it knows its position, reuses terminology from `cross-chapter-state.md`, and opens/closes with transitions to its neighbors. `resolve_write_mode()` in `agents/outline.py` raises `OutlineRouteError` (not a fallback) if the folder is missing from `outline.md` — an out-of-outline folder is not a legal chapter. The single writing contract (`build_mode_clause` / `build_mode_review_clause`) is injected into **every stage that rewrites prose** (1a, 1b–1c, each Stage-3 round, Stage 4); missing one lets chapter prose drift from the cross-chapter contract while the pipeline still prints success.

`--all` requires `workspace/` to match `outline.md` exactly: a missing folder (never `--init`-ed) or an extra folder (renamed-title leftover) aborts before any chapter runs, so `--all` never runs a subset while reporting full completion. `workspace/` is git-tracked via `.gitkeep` only — all chapter artifacts are gitignored runtime output. `workspace/_EXAMPLE-whole-paper/` is a scaffold (single-brief whole-paper demo with per-section `type:`); `_`-prefixed folders are never treated as chapters.

### The pipeline (`agents/orchestrator.py`, `run_4stage_with_progress`)

Orchestration is deterministic Python, not Manager-driven. Per chapter:

```
routing      → (resolve_write_mode / resolve_run_route; pre-flight gates)
Stage 0      → input.md        (Manager, from references/bibliography.md — never overwritten)
              → evidence-pack.md  (STORM-style multi-perspective QA; perspectives switch by family)
Stage 1a     → draft-v1.plan.md  (Manager: plan + frozen Notation/Terminology Table)
Stage 1b~    → draft-v1.part-N.md (part count = min(#sections, 3); each part routed on its own sections)
              → draft-v1.md       (concatenated by Python, using the actual part count)
              → number-check.md   (number gate vs data/ ground truth, strictness by gate level)
Stage 2      → review-v1.md + review-v1.json (MUST FIX frozen checklist; criteria by family)
              → citation-insertions.md
Stage 3      → draft-v2.md       (convergence loop: revise↔verify frozen checklist, ≤ MAX_REVISION_ROUNDS)
Stage 4      → final.md + final.zh.md + decision.md + todo.md (number gate re-run, OVERWRITES number-check.md)
Stage 5      → cross-chapter-state.md  (via cross-chapter-draft.md candidate + atomic replace + Python validation)
```

Key invariants to respect when editing:

- **Every prose-rewriting stage is pointed at `idea.md` directly** via `idea_clause(family)` injected as the first line of the task prompt — the agent reads the full global `idea.md` itself (`read_file`), never a copied snippet. Data/mixed-family stages additionally point at `data/data-index.md`. There is **no `context-pack.md`** anymore; the whole `pack_fingerprint` machinery is gone.
- **Resume = "file exists ⇒ skip."** Every artifact is skipped if present. Route changes are captured by the `outline-fingerprint` in `brief.md` line 1 (from `chapter_fingerprint`, covering type + per-section types + bullets); a changed outline without re-`--init` hard-stops the run with the stale-artifact list — nothing is ever deleted automatically.
- **Convergence acceptance is checklist-derived, not trusted from a top-level boolean.** `_checklist_verdict` requires every frozen MUST FIX id to appear exactly once, resolved, with no unknown/duplicate ids — immune to a hollow `{"all_resolved": true, "items": []}`.
- **Brief-source gates hard-stop before any model call**: folder missing from `outline.md` (`resolve_write_mode` raises), a non-generated brief (no outline fingerprint), a brief stale against the current outline, or a missing `cross-chapter-state.md`.
- **Stage 5 writes `cross-chapter-draft.md` first** and validates it (other chapters' entries preserved line-by-line, own `- [<chapter>] ` claim present in the Key Claims section, three `## ` headings intact) before atomic `os.replace`. Failure blocks `--all`.
- **The number gate runs twice and writes the same `number-check.md`** — Stage 2's review folds the first run's mismatches into MUST FIX, then Stage 4 overwrites the file with the final-draft re-check (there is no separate `number-check-final.md`).

### Anti-hallucination components

- `agents/number_gate.py` — every number in a draft cross-checked against the flattened results store (AI-Scientist-inspired). Prefers a miss over a false alarm: a number is only flagged when adjacent to a metric phrase derived from a result key, compared under percent/fraction scale factors. Both run dir and run file layouts are supported for run identity.
- `agents/citation_check.py` + `latex/build.py` — pre-compile citation closure (in-text `\cite*` vs bib keys) and post-compile log parsing (undefined citations/references). Pure stdlib.
- `agents/citation_supplement.py` — auto-inserts `\cite{key}` only from local bib keys; web-sourced candidates must pass `verify_url`; anything uncertain goes to `needs_human`. Never fabricates a key.
- `agents/retrieval.py` + `agents/tools.py` — two-tier lookup: Tier 1 local `references/bibliography.md` (authoritative, citable), Tier 2 web-search LLM (leads only, explicitly not citable). `RETRIEVAL_MODEL` blank ⇒ web tier disabled. The full reference index is blocked from being read wholesale (`_is_full_reference_index`).

### Content source (`agents/content_source.py`)

No aggregated context pack anymore. This module only reads/tidies raw material: `load_idea_document` / `idea_is_skeleton` (pre-flight gate), and `load_results_store` / `list_plots` / `render_results_summary` (material overview fed to the Manager when it builds `data/data-index.md` at `--init`). `data_dir_has_content` decides whether data-index generation is skipped. Textual run metadata in the results store is marked with a `:` prefix to keep it out of the citable-number table.

## Conventions worth knowing

- Codebase comments and user-facing CLI messages are **predominantly Chinese**; prompts are English. Keep both.
- Agent tools are thin wrappers over `read_file`/`write_file`/`list_folder`/`search_references`/`search_literature` (`agents/tools.py`); they log to stdout with a `[Owner] action | path` shape that the progress filter parses. `set_agent_context()` switches the owner tag.
- `config.py` uses one shared `OPENAI_API_BASE` env var for the LiteLLM model (the per-agent base is written in before each model is built).
- Skill files in `skills/` are read on demand by agents via `read_file` (writing-style, figure-table-placeholder, math-formula, review-rubric, experiment-writing) — do not inline them into prompts.
- `__init__.py` re-exports; modules reach project root via `sys.path.insert` rather than relying on package install.
