"""纯内容阶段:模型只返回文本,读写由脚本完成。"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.content_stage import (ContentAgent, Doc, build_user_message, parse_file_blocks,
                                  run_content_stage)

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


class Msg:
    def __init__(self, c): self.content = c


class ScriptedModel:
    """按顺序返回预设回复,记录收到的消息。"""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def generate(self, messages, **kw):
        self.calls.append([(m.role if hasattr(m, "role") else m["role"],
                            m.content if hasattr(m, "content") else m["content"]) for m in messages])
        return Msg(self.replies.pop(0))


def block(name, body):
    return f'<file name="{name}">\n{body}\n</file>'


def test_parse():
    r = block("a.md", "# A\nbody") + "\nnoise\n" + block("b.md", "B")
    got = parse_file_blocks(r, ["a.md", "b.md"])
    check("解析出两个块", set(got) == {"a.md", "b.md"} and got["a.md"] == "# A\nbody\n")
    check("未声明的文件名被忽略", "c.md" not in parse_file_blocks(block("c.md", "x"), ["a.md"]))
    check("空块不算产物", parse_file_blocks(block("a.md", "  "), ["a.md"]) == {})
    check("同名后者覆盖", parse_file_blocks(block("a.md", "one") + block("a.md", "two"), ["a.md"])["a.md"] == "two\n")
    check("单产物允许无块正文", parse_file_blocks("just text", ["a.md"]) == {"a.md": "just text\n"})
    check("单产物去掉整体围栏", parse_file_blocks("```markdown\nhi\n```", ["a.md"]) == {"a.md": "hi\n"})
    check("多产物不接受无块正文", parse_file_blocks("just text", ["a.md", "b.md"]) == {})
    check("正文里含 <lookup> 时不当正文", parse_file_blocks('<lookup key="k">q</lookup>', ["a.md"]) == {})


def test_message_and_run():
    d = Path(tempfile.mkdtemp())
    (d / "in.md").write_text("INPUT TEXT", encoding="utf-8")
    msg = build_user_message("do it", [Doc("in.md", d / "in.md"), Doc("gone.md", d / "gone.md")], ["out.md"])
    check("材料原文进了提示词", "INPUT TEXT" in msg and '<document name="in.md">' in msg)
    check("缺失材料有标记", 'name="gone.md" status="missing"' in msg)
    check("产物名写进提示词", "out.md" in msg and "<deliverables>" in msg)
    check("材料在任务前", msg.index("<documents>") < msg.index("<task>"))

    m = ScriptedModel([block("out.md", "# Out\ntext") + "\n" + block("todo.md", "No outstanding issues.")])
    out = run_content_stage(ContentAgent(m, "SYS", "Draft"), "task", [Doc("in.md", d / "in.md")],
                            ["out.md", "todo.md"], d)
    check("两个产物都写入", (d / "out.md").read_text() == "# Out\ntext\n" and (d / "todo.md").is_file())
    check("没有缺失", out.missing == [] and out.rounds == 1)
    check("系统提示词原样传入", m.calls[0][0] == ("system", "SYS"))
    check("没有残留临时文件", not list(d.glob("*.tmp")))


def test_retry_only_missing():
    d = Path(tempfile.mkdtemp())
    m = ScriptedModel(["Sure, here is my answer in chat only.",
                       block("a.md", "A"),
                       block("b.md", "B")])
    out = run_content_stage(ContentAgent(m, "S", "Draft"), "t", [], ["a.md", "b.md"], d)
    check("第一轮没块 → 追问缺失项", "a.md, b.md" in m.calls[1][-1][1])
    check("第二轮只补缺的", "b.md" in m.calls[2][-1][1] and "a.md" not in m.calls[2][-1][1].split("for:")[1])
    check("最终全部写出", out.missing == [] and (d / "a.md").is_file() and (d / "b.md").is_file())
    check("共 3 次调用", out.rounds == 3)


def test_gives_up():
    d = Path(tempfile.mkdtemp())
    m = ScriptedModel(["nope"] * 5)
    out = run_content_stage(ContentAgent(m, "S", "Draft"), "t", [], ["a.md", "b.md"], d, rounds=3)
    check("用尽仍缺 → 报缺失而不是抛错", out.missing == ["a.md", "b.md"] and not (d / "a.md").exists())
    check("调用次数受限", len(m.calls) == 3)


def test_protected_names():
    d = Path(tempfile.mkdtemp())
    for bad in ("verification.md", "brief.md", "../x.md", "a/b.md", ".hidden"):
        try:
            run_content_stage(ContentAgent(ScriptedModel(["x"]), "S", "D"), "t", [], [bad], d)
            check(f"拒绝产物名 {bad}", False)
        except ValueError:
            check(f"拒绝产物名 {bad}", True)
    m = ScriptedModel([block("verification.md", "hacked") + block("a.md", "A")])
    run_content_stage(ContentAgent(m, "S", "D"), "t", [], ["a.md"], d)
    check("模型写程序专属文件无效", not (d / "verification.md").exists())


def test_lookup_loop():
    d = Path(tempfile.mkdtemp())
    seen = []

    def lookup(key, query):
        seen.append((key, query))
        return f"EXCERPT for {key}"

    m = ScriptedModel(['<lookup key="resnet">identity shortcut</lookup>', block("a.md", "A uses the excerpt")])
    out = run_content_stage(ContentAgent(m, "S", "D"), "t", [], ["a.md"], d, lookup=lookup)
    check("脚本代查了原文", seen == [("resnet", "identity shortcut")])
    check("摘录放回对话", "EXCERPT for resnet" in m.calls[1][-1][1])
    check("查完得到产物", out.missing == [] and (d / "a.md").is_file())
    m2 = ScriptedModel(['<lookup key="k">q</lookup>'] * 6)
    out2 = run_content_stage(ContentAgent(m2, "S", "D"), "t", [], ["a.md"], d / "x", lookup=lookup,
                             rounds=2, max_lookup_rounds=2)
    check("补查次数有上限", len(m2.calls) <= 4 and out2.missing == ["a.md"])


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_") and callable(f):
            f()
    print(f"\nCONTENT STAGE TESTS PASSED — {len(PASSED)} 项检查全过")
