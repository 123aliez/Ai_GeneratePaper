#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build pipeline for the experiment-paper framework.

Unlike a bare pdflatex run, this build gates on citation integrity BEFORE
compiling, and parses the LaTeX log for undefined references AFTER compiling.
It reuses the deterministic checks in agents/citation_check.py so the same
logic serves both the agent loop and manual builds.

Usage:
    python latex/build.py                 # build latex/main.tex against references.bib
    python latex/build.py --paper NAME    # build workspace/NAME/main.tex if present
    python latex/build.py --check-only    # run the citation gate, do not compile

Exit code is non-zero if the citation gate fails or compilation errors, so this
is safe to call from CI or a pre-submission hook.
"""
import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agents.citation_check import (  # noqa: E402
    run_citation_gate,
    compile_latex,
)

DEFAULT_TEX = HERE / "main.tex"
DEFAULT_BIB = PROJECT_ROOT / "references" / "references.bib"


def resolve_tex(paper: str | None) -> Path:
    if paper:
        candidate = PROJECT_ROOT / "workspace" / paper / "main.tex"
        if candidate.exists():
            return candidate
        print(f"[build] no main.tex under workspace/{paper}; falling back to latex/main.tex")
    return DEFAULT_TEX


def main() -> int:
    parser = argparse.ArgumentParser(description="Experiment-paper LaTeX build with citation gate.")
    parser.add_argument("--paper", help="workspace/<name> whose main.tex to build")
    parser.add_argument("--check-only", action="store_true", help="run the citation gate only, skip pdflatex")
    parser.add_argument("--bib", default=str(DEFAULT_BIB), help="path to references.bib")
    args = parser.parse_args()

    tex_path = resolve_tex(args.paper)
    if not tex_path.exists():
        print(f"[build] ERROR: {tex_path} not found. Create it (or run with --paper).")
        return 2

    # ── Gate: citation closure BEFORE compiling (no log yet) ──
    # run_citation_gate returns (passed: bool, messages: list[str]).
    print(f"[build] citation gate on {tex_path.name} against {Path(args.bib).name}")
    passed, messages = run_citation_gate(str(tex_path), args.bib)
    for line in messages:
        print(f"[gate] {line}")
    if not passed:
        print("[build] citation gate FAILED — fix the issues above before compiling.")
        return 1

    if args.check_only:
        print("[build] gate passed (--check-only, not compiling).")
        return 0

    # ── Compile: compile_latex returns the produced .log path (or None) ──
    print(f"[build] compiling {tex_path.name}")
    log_path = compile_latex(str(tex_path))
    if log_path is None:
        print("[build] WARNING: no log produced (pdflatex missing or compile failed).")
        return 1

    # ── Re-run the gate WITH the log to surface undefined citations/refs ──
    passed_after, messages_after = run_citation_gate(str(tex_path), args.bib, log_path)
    for line in messages_after:
        print(f"[latex] {line}")
    if not passed_after:
        print("[build] post-compile checks reported problems (see above).")
        return 1

    pdf = Path(log_path).with_suffix(".pdf")
    print(f"[build] OK{f' -> {pdf}' if pdf.exists() else ' (no pdf found)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
