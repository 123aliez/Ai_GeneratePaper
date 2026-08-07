"""Pre-compile citation-integrity checks for the experiment-paper framework.

Borrowed in spirit from AI-Scientist's ``generate_latex`` gate: before a LaTeX
document is compiled (or accepted after compilation), verify that every in-text
citation resolves to a bibliography entry and that the compiler did not leave
undefined citations/references behind.

Pure standard library (re, os, pathlib, subprocess). No third-party deps, so it
can be imported from the orchestrator without touching the model/agent stack.

Public API (stable — the orchestrator calls these by exact signature):

    check_citation_closure(tex_text, bib_keys)  -> dict
    extract_bib_keys(bib_text)                   -> set[str]
    parse_latex_log(log_text)                    -> dict
    run_citation_gate(tex_path, bib_path, log_path=None) -> tuple[bool, list[str]]

Everything else (regexes, helpers, the optional ``compile_latex``) is internal
and may change.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# ── Regexes ──────────────────────────────────────────────────────────────
# \cite / \citet / \citep, optional star, optional [pre][post] args, then the
# brace group of comma-separated keys. Keys themselves never contain '}'.
_CITE_RE = re.compile(
    r"\\cite[tp]?\*?\s*(?:\[[^\]]*\]\s*)*\{([^}]*)\}"
)

# @article{KEY,  /  @inproceedings{ KEY ,  — key excludes whitespace/braces/comma.
_BIB_ENTRY_RE = re.compile(r"@\s*[A-Za-z]+\s*\{\s*([^,\s{}]+)\s*,")

# \begin{filecontents}{name.bib} ... \end{filecontents}  (starred variant too).
_FILECONTENTS_RE = re.compile(
    r"\\begin\{filecontents\*?\}\{[^}]*\}(.*?)\\end\{filecontents\*?\}",
    re.DOTALL,
)

# LaTeX log warnings. Quote chars vary (`key', 'key', "key"), so accept a set.
_UNDEF_CITE_RE = re.compile(
    r"Citation\s+[`'\"]([^`'\"]+)[`'\"].*?undefined", re.IGNORECASE
)
_UNDEF_REF_RE = re.compile(
    r"Reference\s+[`'\"]([^`'\"]+)[`'\"].*?undefined", re.IGNORECASE
)
_OVERFULL_RE = re.compile(r"Overfull\s+\\hbox")
_GENERIC_UNDEF_REFS_RE = re.compile(
    r"There were undefined references", re.IGNORECASE
)
_GENERIC_UNDEF_CITES_RE = re.compile(
    r"There were undefined citations", re.IGNORECASE
)
_WARNING_LINE_RE = re.compile(r"Warning")


# ── 1. Citation closure ──────────────────────────────────────────────────
def _extract_cite_keys(tex_text: str) -> set[str]:
    """All keys referenced by \\cite/\\citet/\\citep, comma-groups split out."""
    keys: set[str] = set()
    for group in _CITE_RE.findall(tex_text or ""):
        for raw in group.split(","):
            key = raw.strip()
            if key:
                keys.add(key)
    return keys


def check_citation_closure(tex_text: str, bib_keys: set[str]) -> dict:
    """Compare in-text citations against the available bibliography keys.

    Args:
        tex_text: Full LaTeX source (or any text carrying \\cite* commands).
        bib_keys: Set of keys defined in the bibliography.

    Returns:
        {"dangling": sorted list of cited-but-undefined keys,
         "unused":   sorted list of defined-but-uncited keys,
         "ok":       True iff there are no dangling citations}
    """
    bib_keys = set(bib_keys or set())
    cited = _extract_cite_keys(tex_text)
    dangling = sorted(cited - bib_keys)
    unused = sorted(bib_keys - cited)
    return {"dangling": dangling, "unused": unused, "ok": not dangling}


# ── 2. Bibliography keys ─────────────────────────────────────────────────
def extract_bib_keys(bib_text: str) -> set[str]:
    """Extract entry keys from BibTeX text.

    Handles both a plain ``.bib`` body (``@article{KEY, ...}``) and keys wrapped
    in a ``\\begin{filecontents}{...} ... \\end{filecontents}`` block (the form
    AI-Scientist embeds inside the main ``.tex``). Because ``filecontents`` bodies
    use the same ``@type{key,`` syntax, a whole-text scan already captures them;
    the block extraction below is a redundant safety net for oddly nested cases.
    """
    bib_text = bib_text or ""
    keys: set[str] = set(_BIB_ENTRY_RE.findall(bib_text))
    for block in _FILECONTENTS_RE.findall(bib_text):
        keys.update(_BIB_ENTRY_RE.findall(block))
    return keys


# ── 3. LaTeX log parsing ─────────────────────────────────────────────────
def parse_latex_log(log_text: str) -> dict:
    """Scan a LaTeX compile log for citation/reference problems.

    Args:
        log_text: Raw contents of the ``.log`` file (or captured stdout).

    Returns:
        {"undefined_citations": sorted unique keys the compiler could not resolve,
         "undefined_references": sorted unique undefined \\ref targets,
         "overfull_count":       number of "Overfull \\hbox" occurrences,
         "warnings":             de-duplicated warning lines (order preserved),
         "ok":                   True iff no undefined citations or references}

    Note: LaTeX also emits summary lines like "There were undefined references."
    without naming a key (e.g. when the detailed line was truncated). If such a
    summary appears but no named entry was captured, a sentinel is added to the
    matching list so ``ok`` correctly reports False.
    """
    log_text = log_text or ""

    undefined_citations = sorted(set(_UNDEF_CITE_RE.findall(log_text)))
    undefined_references = sorted(set(_UNDEF_REF_RE.findall(log_text)))
    overfull_count = len(_OVERFULL_RE.findall(log_text))

    # Generic summaries with no named key — keep ``ok`` honest.
    if _GENERIC_UNDEF_CITES_RE.search(log_text) and not undefined_citations:
        undefined_citations = ["<unnamed: 'There were undefined citations'>"]
    if _GENERIC_UNDEF_REFS_RE.search(log_text) and not undefined_references:
        undefined_references = ["<unnamed: 'There were undefined references'>"]

    warnings: list[str] = []
    seen: set[str] = set()
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped and _WARNING_LINE_RE.search(stripped) and stripped not in seen:
            seen.add(stripped)
            warnings.append(stripped)

    ok = not undefined_citations and not undefined_references
    return {
        "undefined_citations": undefined_citations,
        "undefined_references": undefined_references,
        "overfull_count": overfull_count,
        "warnings": warnings,
        "ok": ok,
    }


# ── file IO (graceful, never raises) ─────────────────────────────────────
def _read_text(path: str | os.PathLike) -> tuple[str | None, str | None]:
    """Return (text, error). On failure text is None and error is a message."""
    try:
        p = Path(path)
        if not p.exists():
            return None, f"file not found: {p}"
        if p.is_dir():
            return None, f"expected a file but got a directory: {p}"
        return p.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:  # permission, decode-with-replace won't raise, etc.
        return None, f"could not read {path}: {exc}"


# ── 4. Gate ──────────────────────────────────────────────────────────────
def run_citation_gate(
    tex_path: str, bib_path: str, log_path: str | None = None
) -> tuple[bool, list[str]]:
    """Run the full pre-compile citation gate over on-disk files.

    Reads the ``.tex`` and ``.bib`` (and optionally a compile ``.log``), runs the
    closure and log checks, and returns a human-readable problem list. Missing or
    unreadable files degrade gracefully into ``[ERROR]`` messages and a failing
    gate rather than raising.

    Args:
        tex_path: Path to the LaTeX source.
        bib_path: Path to the BibTeX file (or a .tex carrying filecontents).
        log_path: Optional path to a compile log for undefined-* detection.

    Returns:
        (passed, messages). ``passed`` is False if there are dangling citations or
        the log reports undefined citations/references, or if a required file
        could not be read. ``unused`` entries, overfull boxes, and generic
        warnings are reported as informational lines and never fail the gate.
    """
    messages: list[str] = []
    passed = True

    tex_text, tex_err = _read_text(tex_path)
    bib_text, bib_err = _read_text(bib_path)

    if tex_err:
        messages.append(f"[ERROR] tex: {tex_err}")
        passed = False
    if bib_err:
        messages.append(f"[ERROR] bib: {bib_err}")
        passed = False

    # Closure check only makes sense when both sources are available.
    if tex_text is not None and bib_text is not None:
        bib_keys = extract_bib_keys(bib_text)
        if not bib_keys:
            messages.append("[WARNING] no bibliography keys parsed from bib file")
        closure = check_citation_closure(tex_text, bib_keys)
        for key in closure["dangling"]:
            messages.append(f"[DANGLING] cited but not in bibliography: {key}")
        for key in closure["unused"]:
            messages.append(f"[UNUSED] in bibliography but never cited: {key}")
        if not closure["ok"]:
            passed = False

    # Optional log analysis.
    if log_path:
        log_text, log_err = _read_text(log_path)
        if log_err:
            messages.append(f"[WARNING] log: {log_err}")
        else:
            log = parse_latex_log(log_text)
            for key in log["undefined_citations"]:
                messages.append(f"[UNDEFINED-CITE] compiler could not resolve: {key}")
            for key in log["undefined_references"]:
                messages.append(f"[UNDEFINED-REF] compiler could not resolve: {key}")
            if log["overfull_count"]:
                messages.append(f"[OVERFULL] {log['overfull_count']} overfull hbox(es)")
            if not log["ok"]:
                passed = False

    if passed and not any(
        m.startswith(("[DANGLING]", "[UNDEFINED-CITE]", "[UNDEFINED-REF]"))
        for m in messages
    ):
        messages.insert(0, "[OK] citation gate passed")

    return passed, messages


# ── optional: drive a real compile to produce a log (best-effort) ─────────
def compile_latex(
    tex_path: str, engine: str = "pdflatex", timeout: int = 120
) -> str | None:
    """Best-effort compile to (re)generate a ``.log`` next to the source.

    Not part of the gate contract and never called by the self-test — provided so
    the orchestrator can obtain a log path when one does not already exist. Runs
    the engine in the tex file's directory and returns the log path if produced,
    otherwise None. Never raises.
    """
    try:
        tex = Path(tex_path)
        if not tex.exists():
            return None
        subprocess.run(
            [engine, "-interaction=nonstopmode", "-halt-on-error", tex.name],
            cwd=str(tex.parent),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        log = tex.with_suffix(".log")
        return str(log) if log.exists() else None
    except (OSError, subprocess.SubprocessError):
        return None


# ── self-test ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== citation_check self-test ===\n")

    # Sample A: a dirty document — cites a missing key, leaves one bib unused.
    tex_dirty = r"""
    \documentclass{article}
    \begin{document}
    Scaling laws \citep{chinchilla} and data curation \citet{dclm, refinedweb}
    matter; see also \cite{ghost2024} for the missing one.
    \bibliography{refs}
    \end{document}
    """
    bib_dirty = r"""
    @article{chinchilla, title={Chinchilla}, year={2022}}
    @inproceedings{ dclm , title={DCLM}, year={2024}}
    @article{refinedweb, title={RefinedWeb}, year={2023}}
    @misc{neverCited, title={Orphan Entry}, year={2020}}
    """
    keys_dirty = extract_bib_keys(bib_dirty)
    closure_dirty = check_citation_closure(tex_dirty, keys_dirty)
    print("[A] extract_bib_keys ->", sorted(keys_dirty))
    print("[A] closure          ->", closure_dirty)
    assert closure_dirty["dangling"] == ["ghost2024"], closure_dirty
    assert closure_dirty["unused"] == ["neverCited"], closure_dirty
    assert closure_dirty["ok"] is False
    print("[A] PASS: dangling 'ghost2024' + unused 'neverCited' detected\n")

    # Sample B: a clean document — every cite resolves, nothing orphaned.
    tex_clean = r"""
    \documentclass{article}\begin{document}
    See \citep{gpt3} and \citet[see][p.4]{llama3}.
    \end{document}
    """
    bib_clean = r"""
    \begin{filecontents}{refs.bib}
    @article{gpt3,   title={GPT-3},   year={2020}}
    @article{llama3, title={Llama 3}, year={2024}}
    \end{filecontents}
    """
    keys_clean = extract_bib_keys(bib_clean)
    closure_clean = check_citation_closure(tex_clean, keys_clean)
    print("[B] extract_bib_keys (filecontents) ->", sorted(keys_clean))
    print("[B] closure                         ->", closure_clean)
    assert keys_clean == {"gpt3", "llama3"}, keys_clean
    assert closure_clean["dangling"] == []
    assert closure_clean["ok"] is True
    print("[B] PASS: clean doc, filecontents keys extracted, no dangling\n")

    # Sample C: a LaTeX log with undefined citation + reference + overfull boxes.
    log_dirty = r"""
    This is pdfTeX, Version 3.14
    LaTeX Warning: Citation `ghost2024' on page 1 undefined on input line 12.
    LaTeX Warning: Reference `fig:missing' on page 2 undefined on input line 40.
    Overfull \hbox (12.34pt too wide) in paragraph at lines 10--12
    Overfull \hbox (5.0pt too wide) in paragraph at lines 20--22
    Package hyperref Warning: Token not allowed in a PDF string.
    LaTeX Warning: There were undefined references.
    """
    parsed = parse_latex_log(log_dirty)
    print("[C] parse_latex_log ->")
    print("      undefined_citations :", parsed["undefined_citations"])
    print("      undefined_references:", parsed["undefined_references"])
    print("      overfull_count      :", parsed["overfull_count"])
    print("      warnings            :", len(parsed["warnings"]), "line(s)")
    print("      ok                  :", parsed["ok"])
    assert parsed["undefined_citations"] == ["ghost2024"], parsed
    assert parsed["undefined_references"] == ["fig:missing"], parsed
    assert parsed["overfull_count"] == 2, parsed
    assert parsed["ok"] is False
    print("[C] PASS: undefined cite/ref + 2 overfull boxes parsed\n")

    # Sample D: a clean log — no undefined anything.
    log_clean = "LaTeX Warning: Font shape undefined, using default.\nOutput written on main.pdf.\n"
    parsed_clean = parse_latex_log(log_clean)
    assert parsed_clean["ok"] is True, parsed_clean
    assert parsed_clean["undefined_citations"] == []
    assert parsed_clean["overfull_count"] == 0
    print("[D] parse_latex_log (clean) ->", parsed_clean["ok"], "(ok)\n")

    # Sample E: gate over temp files — dirty tex+bib+log should fail with messages.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tp = Path(tmp) / "main.tex"
        bp = Path(tmp) / "refs.bib"
        lp = Path(tmp) / "main.log"
        tp.write_text(tex_dirty, encoding="utf-8")
        bp.write_text(bib_dirty, encoding="utf-8")
        lp.write_text(log_dirty, encoding="utf-8")

        passed, msgs = run_citation_gate(str(tp), str(bp), str(lp))
        print("[E] run_citation_gate(dirty) -> passed =", passed)
        for m in msgs:
            print("      ", m)
        assert passed is False
        assert any(m.startswith("[DANGLING]") for m in msgs)
        assert any(m.startswith("[UNDEFINED-CITE]") for m in msgs)
        assert any(m.startswith("[UNDEFINED-REF]") for m in msgs)
        print("[E] PASS: gate failed with dangling + undefined messages\n")

        # Missing file path -> graceful failure, no exception.
        passed_missing, msgs_missing = run_citation_gate(
            str(Path(tmp) / "nope.tex"), str(bp)
        )
        assert passed_missing is False
        assert any(m.startswith("[ERROR]") for m in msgs_missing)
        print("[F] run_citation_gate(missing tex) -> passed =", passed_missing,
              "| ", msgs_missing[0])

    print("\n=== all self-tests passed ===")
