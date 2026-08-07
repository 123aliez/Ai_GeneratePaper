"""Offline routing tests — no API calls, no real experiment data.

Covers the two decisions the framework now hinges on:
  1. `type:` in brief.md decides evidence source and number-gate strictness.
  2. A global idea.md is the primary input for idea-family chapters.

Run: python tests/test_routing.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.chapter_type import (
    parse_brief_type, resolve_run_route, route_banner, normalize_type,
    IDEA, DATA, MIXED, BLOCKING, ADVISORY, OFF, DEFAULT_TYPE,
)
from agents.orchestrator import (
    parse_brief_sections, build_stage1_parts, part_family,
    build_routing_clause, build_review_routing_clause,
)
from agents.evidence_mining import perspectives_for_mode, build_evidence_mining_prompt
from agents.content_source import build_context_pack, load_idea_document

PASSED = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def make_workspace(brief: str, name: str = "chapter") -> str:
    folder = Path(tempfile.mkdtemp()) / name
    folder.mkdir(parents=True)
    (folder / "brief.md").write_text(brief, encoding="utf-8")
    return str(folder)


# ── 1. type: routes evidence source and gate ────────────────────────────
def test_type_routes_gate():
    cases = [
        ("method", IDEA, ADVISORY),
        ("results", DATA, BLOCKING),
        ("ablation", DATA, BLOCKING),
        ("related work", IDEA, OFF),
        ("conclusion", MIXED, ADVISORY),
    ]
    for declared, want_family, want_gate in cases:
        folder = make_workspace(f"# B\n\ntype: {declared}\n\n1. **S** (~300 words)\n- x\n")
        info = parse_brief_type(folder)
        check(f"type '{declared}' → {want_family}/{want_gate}",
              (info["family"], info["gate"]) == (want_family, want_gate),
              f"got {info['family']}/{info['gate']}")


def test_chinese_and_decorated_aliases():
    for declared, want in [("方法", "method"), ("消融实验", "ablation"),
                           ("Related-Work", "related"), ("Ablation Study", "ablation"),
                           ("EXPERIMENTS", "experiments")]:
        got = normalize_type(declared)
        check(f"alias '{declared}' → {want}", got == want, f"got {got}")


def test_unknown_type_degrades_and_reports():
    folder = make_workspace("# B\n\ntype: bogus-section\n")
    info = parse_brief_type(folder)
    check("unknown type degrades to mixed/advisory",
          (info["type"], info["family"], info["gate"]) == (DEFAULT_TYPE, MIXED, ADVISORY),
          str(info))
    check("unknown type is reported for the user",
          info["unrecognized"] == "bogus-section", str(info))


def test_folder_name_fallback():
    folder = make_workspace("# B\n\n## Task\nno type line\n", name="05-results")
    info = parse_brief_type(folder)
    check("folder-name fallback infers results",
          (info["type"], info["source"], info["gate"]) == ("results", "folder", BLOCKING),
          str(info))


def test_template_placeholder_is_not_a_declaration():
    # The shipped template writes `type: <method>`; the angle brackets mark it as
    # a placeholder, and treating it as a real declaration would silently route
    # every copied-but-unedited brief as a method chapter.
    folder = make_workspace("# B\n\ntype: <method>\n")
    info = parse_brief_type(folder)
    check("placeholder '<method>' is not a declaration",
          info["source"] == "default", str(info))


# ── 2. whole-paper brief: per-section and per-part routing ──────────────
WHOLE_PAPER = """# Test paper

type: method

1. **Abstract** (~150 words)
- headline

2. **Introduction** (~250 words)
- motivation

3. **Related Work** (~200 words)
- prior art

4. **Method** (~300 words)
- design

5. **Results** (~300 words)
- numbers

6. **Conclusion** (~150 words)
- takeaway
"""


def test_whole_paper_unions_to_blocking():
    folder = make_workspace(WHOLE_PAPER)
    sections = parse_brief_sections(folder)
    route = resolve_run_route(folder, sections)
    check("whole-paper brief parses 6 sections", len(sections) == 6, str(len(sections)))
    check("whole-paper route is mixed", route["family"] == MIXED, str(route))
    check("whole-paper gate blocks (a data section exists)",
          route["gate"] == BLOCKING, str(route))
    check("section types inferred from titles",
          route["section_types"] == {1: "abstract", 2: "intro", 3: "related",
                                    4: "method", 5: "results", 6: "conclusion"},
          str(route["section_types"]))


def test_part_family_splits_by_section():
    folder = make_workspace(WHOLE_PAPER)
    sections = parse_brief_sections(folder)
    route = resolve_run_route(folder, sections)
    parts = build_stage1_parts(sections)
    families = [part_family(p, route) for p in parts]
    check("part 1 (abstract+intro) routes to idea", families[0] == IDEA, str(families))
    check("part 2 (related+method) routes to idea", families[1] == IDEA, str(families))
    check("part 3 (results+conclusion) is mixed", families[2] == MIXED, str(families))
    # The method part must be told the idea document is primary, not the numbers.
    clause = build_routing_clause(route, families[1], parts[1])
    check("method part's prompt names the idea block as primary",
          "'## Core idea' block" in clause and "IDEA chapter" in clause, clause[:200])
    check("method part's prompt demotes the results table",
          "supporting evidence" in clause, clause[:300])


def test_idea_only_paper_never_blocks():
    folder = make_workspace(
        "type: method\n\n1. **Method** (~300 words)\n- x\n\n"
        "2. **Related Work** (~200 words)\n- y\n")
    route = resolve_run_route(folder, parse_brief_sections(folder))
    check("idea-only paper routes to idea", route["family"] == IDEA, str(route))
    check("idea-only paper gate is advisory, not blocking",
          route["gate"] == ADVISORY, str(route))


def test_section_override_beats_title():
    folder = make_workspace(
        "type: method\n\n1. **Deep Dive** (~300 words)\n- type: results\n- x\n")
    route = resolve_run_route(folder, parse_brief_sections(folder))
    check("explicit section override wins over the title guess",
          route["section_types"] == {1: "results"}, str(route))
    check("a data section forces the gate to blocking",
          route["gate"] == BLOCKING, str(route))


# ── 3. evidence mining and review criteria follow the family ────────────
def test_perspectives_differ_by_family():
    idea_names = [n for n, _ in perspectives_for_mode(IDEA)]
    data_names = [n for n, _ in perspectives_for_mode(DATA)]
    check("idea perspectives ask about mechanism/novelty",
          any("Mechanism" in n for n in idea_names) and any("Novelty" in n for n in idea_names),
          str(idea_names))
    check("idea perspectives do NOT ask for statistics",
          not any("Statistics" in n for n in idea_names), str(idea_names))
    check("data perspectives ask about statistics/baselines",
          any("Statistics" in n for n in data_names)
          and any("Baseline" in n for n in data_names), str(data_names))
    check("families get different perspectives", idea_names != data_names)


def test_evidence_prompt_grounding_rule_matches_family():
    idea_prompt = build_evidence_mining_prompt("Method", "PACK", "", "/tmp/x", IDEA)
    data_prompt = build_evidence_mining_prompt("Results", "PACK", "", "/tmp/x", DATA)
    check("idea mining grounds answers in the author's design statement",
          "'## Core idea' block" in idea_prompt, idea_prompt[:400])
    check("data mining grounds answers in logged values",
          "results table" in data_prompt, data_prompt[:400])


def test_review_criteria_follow_family():
    idea_route = {"type": "method", "family": IDEA, "gate": ADVISORY}
    data_route = {"type": "results", "family": DATA, "gate": BLOCKING}
    idea_clause = build_review_routing_clause(idea_route)
    data_clause = build_review_routing_clause(data_route)
    check("reviewer is told not to demand numbers from a method chapter",
          "Do NOT demand more experimental numbers" in idea_clause, idea_clause[:400])
    check("reviewer is told an advisory gate means an empty store is not a defect",
          "ADVISORY" in idea_clause, idea_clause)
    check("reviewer is told to check every number against the store for a data chapter",
          "results table" in data_clause, data_clause[:400])


# ── 4. context pack presents the right primary source ───────────────────
def test_context_pack_promotes_the_right_source():
    idea_file = _write_temp_idea(
        "# Idea — Spec Module\n\n"
        "## Contribution\nOur Spec module rescales spectral channels with negligible "
        "parameter overhead, improving fine-grained classification accuracy.\n\n"
        "## Key insight\nDiscriminative information for fine-grained categories "
        "concentrates in the mid-to-high frequency bands, so recalibrating channels "
        "in the frequency domain amplifies exactly that signal.\n\n"
        "## Design\nThe input feature map is transformed with an FFT, per-channel band "
        "energy is pooled, a two-layer MLP produces gating coefficients, and the "
        "result is transformed back to the spatial domain.\n")
    check("idea document loads", "Spec module" in load_idea_document(idea_file))

    idea_pack = build_context_pack("Method", family=IDEA, idea_path=idea_file)
    data_pack = build_context_pack("Results", family=DATA, idea_path=idea_file)

    check("idea pack leads with the core idea",
          idea_pack.index("## Core idea") < idea_pack.index("## Experiment results"),
          "ordering wrong")
    check("idea pack carries the author's own text",
          "Spec module rescales" in idea_pack)
    check("idea pack demotes results to supporting evidence",
          "SUPPORTING EVIDENCE ONLY" in idea_pack)
    check("data pack leads with the results",
          data_pack.index("## Experiment results") < data_pack.index("## Core idea"),
          "ordering wrong")
    check("data pack marks results primary",
          "THE PRIMARY SOURCE" in data_pack.split("## Core idea")[0])


def test_unfilled_skeleton_counts_as_missing():
    # The pre-flight gate checks that idea.md exists. A file that is 100% template
    # is the dangerous case: the run proceeds and the drafter mistakes the
    # template's own questions for the paper's claims.
    from agents.content_source import idea_is_skeleton
    skeleton = (
        "# Idea — <方法名>\n\n"
        "> **状态:未填写**(填完请删掉这一行)\n\n"
        "## 1. 一句话贡献\n\n"
        "> 整篇论文的贡献压缩成一句。\n\n"
        "## 4.2 关键组件\n\n"
        "| 组件 | 输入 | 输出 | 作用 |\n|---|---|---|---|\n|  |  |  |  |\n\n"
        "## 4.3 公式与符号\n\n- $$:\n- $$:\n\n"
        "## 6. 贡献清单\n\n1.\n2.\n3.\n"
    )
    is_skel, words = idea_is_skeleton(skeleton)
    check("an untouched template is detected as a skeleton", is_skel, f"words={words}")
    check("skeleton word count excludes prompts and empty rows", words < 40, f"words={words}")

    filled = (
        "# Idea — Spec Module\n\n"
        "## 1. 一句话贡献\n"
        "我们提出 Spec 模块,一种谱域重标定单元,在几乎不增参数的前提下提升细粒度分类精度。\n\n"
        "## 3. 核心洞察\n"
        "卷积在空间域做加权,对通道间的频率响应差异不敏感;我们观察到细粒度类别的判别信息\n"
        "集中在中高频带,因此在频域做通道重标定可以直接放大这部分信号。\n\n"
        "## 4. 方法设计\n"
        "输入特征图先做 FFT,按通道计算频带能量,过两层 MLP 得到门控系数,再逆变换回空间域。\n"
    )
    is_skel, words = idea_is_skeleton(filled)
    check("a genuinely written idea document is not a skeleton", not is_skel, f"words={words}")

    empty_pack = build_context_pack("Method", family=IDEA,
                                    idea_path=_write_temp_idea(skeleton))
    check("skeleton context pack announces the template is unfilled",
          "TEMPLATE NOT FILLED IN" in empty_pack, empty_pack[:300])
    check("skeleton context pack forbids answering the template's own questions",
          "Do NOT answer those questions yourself" in empty_pack, empty_pack[:600])


def _write_temp_idea(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "idea.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_idea_document_is_declared_not_invented():
    missing = Path(tempfile.mkdtemp()) / "absent.md"
    pack = build_context_pack("Method", family=IDEA, idea_path=missing)
    check("missing idea document is announced", "## Core idea (MISSING)" in pack, pack[:400])
    check("missing idea document forbids inventing a contribution",
          "Do NOT invent a contribution" in pack or "Do not invent a contribution" in pack,
          pack[:600])
    check("missing idea document names the marker to write",
          "[IDEA NEEDED]" in pack, pack[:600])


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(PASSED)} checks passed across {len(tests)} tests:")
    for label in PASSED:
        print(f"  ok  {label}")
    print("\nROUTING TESTS PASSED")
