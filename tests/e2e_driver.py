"""端到端测试的子进程驱动(不是测试本身):用假模型跑 run.py 的真实命令流程。

用法: python tests/e2e_driver.py <项目路径> <run.py 其余参数...>
PAPER_PROJECT 必须在 import config 之前就位,所以测试用子进程调用本脚本。
"""
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

project = sys.argv[1]
os.environ["PAPER_PROJECT"] = project
sys.argv = ["run.py", "--project", project, *sys.argv[2:]]

try:
    import smolagents  # noqa: F401
except ModuleNotFoundError:  # pragma: no cover
    stub = types.ModuleType("smolagents")
    stub.tool = lambda fn: fn
    stub.CodeAgent = object
    stub.Model = object
    stub.OpenAIModel = object
    sys.modules["smolagents"] = stub

import config                                             # noqa: E402
import agents                                             # noqa: E402
import agents.outline_expand as oe                        # noqa: E402
from agents.outline import SHORT_CHAPTER_TYPES, parse_outline   # noqa: E402
from test_pipeline_routing import FullRunAgent            # noqa: E402

CAPTURED = {}


def _wrap_prompts():
    orig_expand, orig_translate = oe.build_expand_prompt, oe.build_translate_prompt

    def expand(chapters, out_en, out_zh, idea, n):
        CAPTURED["expand"] = (out_en, out_zh)
        return orig_expand(chapters, out_en, out_zh, idea, n)

    def translate(zh, out_en, old_en=""):
        CAPTURED["translate"] = (zh, out_en)
        return orig_translate(zh, out_en, old_en)

    oe.build_expand_prompt, oe.build_translate_prompt = expand, translate


class FakePlanner:
    """--expand / 翻译用的假规划者。"""
    _offline_test_stub = True

    def run(self, prompt, **kwargs):
        if "expand" in CAPTURED and not Path(CAPTURED["expand"][0]).exists():
            en, zh = CAPTURED["expand"]
            chapters = parse_outline(config.INBOX_ROOT / "outline_draft.md")
            for c in chapters:
                if c["type"] in SHORT_CHAPTER_TYPES:
                    c["sections"] = [{"number": 1, "title": c["title"], "target_words": 150,
                                      "words_explicit": True, "type": "",
                                      "bullets": [f"point about {c['title']}"], "synthetic": True}]
                else:
                    c["sections"] = [{"number": 1, "title": f"{c['title']} part", "target_words": 200,
                                      "words_explicit": True, "type": "",
                                      "bullets": ["first point", "second point"]}]
            Path(en).parent.mkdir(parents=True, exist_ok=True)
            Path(en).write_text(oe.render_outline(chapters, "T", oe.EN_EXPANDED_MARKER), encoding="utf-8")
            Path(zh).write_text(oe.render_outline(chapters, "T", oe.ZH_EXPANDED_MARKER), encoding="utf-8")
        elif "translate" in CAPTURED:
            zh, cand = CAPTURED.pop("translate")
            Path(cand).write_text(Path(zh).read_text(encoding="utf-8"), encoding="utf-8")
        return "ok"


def _res_id(suffix: str) -> str:
    text = config.RES_IDS_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        m = re.match(r"\|\s*\{\{(RES-\d+)\}\}\s*\|\s*([^|]+?)\s*\|", line)
        if m and m.group(2).endswith(suffix):
            return m.group(1)
    raise SystemExit(f"no RES id for {suffix}")


class FakeChapterAgent(FullRunAgent):
    ARTIFACTS = FullRunAgent.ARTIFACTS + ("input.md",)

    def run(self, prompt, **kwargs):
        result = super().run(prompt, **kwargs)
        m = re.search(r"'([^']*)/final\.md' \(English\)", prompt)
        if m:
            folder = Path(m.group(1))
            name = folder.name
            top1_ours = _res_id("Ours.Top-1↑")
            top1_base = _res_id("ResNet-50.Top-1↑")
            if "abstract" in name:
                en = (f"# Abstract\n\nWe reach {{{{{top1_ours}}}}}% top-1, "
                      f"{{{{DIFF:{top1_ours},{top1_base}}}}} points above the baseline.\n")
            elif "method" in name:
                en = ("# Method\n\n## Overview\n\nWe build on residual learning \\cite{he2016resnet} "
                      "(see [[FIG:overview]]). The default learning rate is 0.1 [FROM CODE — 需核对].\n\n"
                      "[FIGURE overview: Architecture overview]\nType: diagram\n"
                      "Note. Modules A and B and the data flow.\nSource: idea.md section 4.1\n"
                      "Supports: the design claim\n")
            elif "result" in name:
                en = ("# Results\n\nTable [[TAB:主结果]] compares the methods.\n\n"
                      "[TABLE: 主结果 | caption: Main results on the benchmark. | note: Best in bold.]\n\n"
                      f"Gain over baseline: {{{{REL:{top1_ours},{top1_base}}}}}%.\n")
            else:
                en = f"# {name}\n\nPlain text for {name}.\n"
            folder.joinpath("final.md").write_text(en, encoding="utf-8")
            folder.joinpath("final.zh.md").write_text("中文:" + en, encoding="utf-8")
        return result


_wrap_prompts()
agents.create_planner_agent = lambda model: FakePlanner()
import run                                               # noqa: E402

run.get_draft_model = run.get_review_model = run.get_manager_model = lambda: None
FOLDER_HOLDER = {}


def _create_agents(*_models):
    # 每章一个 agent 实例太重;FullRunAgent 需要 folder,这里用代理按提示词里的章节目录实例化。
    class Router:
        _offline_test_stub = True

        def __init__(self):
            self._by_folder = {}

        def run(self, prompt, **kwargs):
            m = re.search(r"'([^']*workspace/[^'/]+)/(?:brief|input|draft|review|final|evidence|todo|"
                          r"decision|number|cross-chapter-draft)[^']*'", prompt)
            folder = m.group(1) if m else ""
            if folder not in self._by_folder:
                self._by_folder[folder] = FakeChapterAgent(folder)
            return self._by_folder[folder].run(prompt, **kwargs)

    r = Router()
    return r, r, r


run.create_agents = _create_agents
run.main()
