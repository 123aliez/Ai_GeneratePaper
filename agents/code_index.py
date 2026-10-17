"""实验代码的静态索引(纯标准库,只解析、不导入、不执行)。

作者把实验代码整个文件夹放进 inbox/code/。这里只做 `ast.parse` 与文本读取,
生成 code-index.md:文件树、入口脚本、命令行参数默认值、配置文件里的超参,
每项都带 `文件:行号`。它只供实验设置类章节参考——代码里的默认值不等于实际跑实验
时用的值,所以引用它的句子必须标 `[FROM CODE — 需核对]`,并进待补清单。
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
             ".idea", ".vscode", ".ipynb_checkpoints", "wandb", "runs", "logs", "checkpoints",
             "outputs", "output", ".mypy_cache", ".pytest_cache", "build", "dist"}
SKIP_EXTS = {".pt", ".pth", ".ckpt", ".bin", ".safetensors", ".h5", ".npy", ".npz", ".pkl",
             ".pickle", ".onnx", ".zip", ".tar", ".gz", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
             ".csv", ".tsv", ".parquet", ".mat", ".log", ".so", ".pyc", ".whl"}
CONFIG_EXTS = {".yaml", ".yml", ".toml", ".cfg", ".ini", ".json"}
MAX_PY_BYTES = 400_000
MAX_CONFIG_BYTES = 20_000
MAX_CONFIG_LINES = 60
MAX_TREE_FILES = 300
MAX_DEFS_PER_FILE = 25
MAX_INDEX_CHARS = 60_000
ENTRY_NAMES = {"main", "train", "run", "eval", "evaluate", "test", "infer", "inference", "demo",
               "finetune", "pretrain", "preprocess", "prepare"}


def _walk(root: Path):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(files):
            if fn.startswith("."):
                continue
            yield Path(dirpath) / fn


def _literal(node) -> str:
    try:
        return repr(ast.literal_eval(node))
    except Exception:
        try:
            return ast.unparse(node)
        except Exception:
            return "?"


def _argparse_args(tree: ast.AST) -> list[dict]:
    found = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args):
            names = [a.value for a in node.args
                     if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if not names:
                continue
            info = {"names": names, "line": node.lineno, "default": None, "type": None,
                    "help": None, "choices": None}
            for kw in node.keywords:
                if kw.arg in ("default", "type", "help", "choices"):
                    info[kw.arg] = _literal(kw.value)
            found.append(info)
    return sorted(found, key=lambda i: i["line"])


def _summarize_py(path: Path, rel: str) -> dict | None:
    try:
        if path.stat().st_size > MAX_PY_BYTES:
            return {"rel": rel, "skipped": f"文件过大({path.stat().st_size} 字节)"}
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)              # 只解析,绝不执行
    except (SyntaxError, ValueError, OSError) as exc:
        return {"rel": rel, "skipped": f"解析失败: {type(exc).__name__}"}
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    has_main = any(
        isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"
        for n in tree.body)
    defs = []
    for n in tree.body:
        if isinstance(n, ast.ClassDef):
            defs.append((n.lineno, f"class {n.name}"))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append((n.lineno, f"def {n.name}"))
    return {"rel": rel, "doc": doc[0] if doc else "", "has_main": has_main,
            "defs": defs, "args": _argparse_args(tree)}


_SECRET_RE = re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|credential)")


def _redact(line: str) -> str:
    """配置行里疑似密钥的值抹掉(代码索引会进提示词)。"""
    if _SECRET_RE.search(line):
        return re.sub(r"([:=]\s*).+$", r"\1***", line)
    return line


def _summarize_config(path: Path, rel: str) -> dict | None:
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return {"rel": rel, "skipped": f"配置文件过大({path.stat().st_size} 字节)"}
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = []
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            return {"rel": rel, "skipped": "JSON 解析失败"}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    lines.append((0, _redact(f"{k}: {v!r}")))
        else:
            return None
    else:
        for i, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";", "//")):
                continue
            lines.append((i, _redact(stripped)))
    if not lines:
        return None
    return {"rel": rel, "lines": lines[:MAX_CONFIG_LINES], "more": max(0, len(lines) - MAX_CONFIG_LINES)}


def build_code_index(code_root) -> str:
    """生成 code-index.md 文本。code_root 不存在/为空 → 返回说明性的短文。"""
    root = Path(code_root)
    header = [
        "# 代码索引(程序生成,勿手改;源头是 inbox/code/)",
        "",
        "**只供实验设置类章节参考。** 这是静态解析的结果(代码没有被运行)。代码里的默认值、",
        "配置文件里的数值不等于实际跑实验时用的值——正文引用这里的任何细节,必须在句尾标",
        "`[FROM CODE — 需核对]`,并记入待补清单。数字结果只认结果表。",
        "",
    ]
    if not root.is_dir():
        return "\n".join(header + ["(没有 inbox/code/ 文件夹)"]) + "\n"
    files = [p for p in _walk(root) if p.suffix.lower() not in SKIP_EXTS]
    if not files:
        return "\n".join(header + ["(inbox/code/ 里没有可索引的代码文件)"]) + "\n"

    tree_lines = [f"## 文件树({len(files)} 个文件)", ""]
    for p in files[:MAX_TREE_FILES]:
        tree_lines.append(f"- {p.relative_to(root).as_posix()}")
    if len(files) > MAX_TREE_FILES:
        tree_lines.append(f"- …另有 {len(files) - MAX_TREE_FILES} 个文件未列出")

    py_sections, entries, cfg_sections, skipped_notes = [], [], [], []
    for p in files:
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        if ext == ".py":
            info = _summarize_py(p, rel)
            if info is None:
                continue
            if "skipped" in info:
                skipped_notes.append(f"- {rel}: {info['skipped']}")
                continue
            if info["has_main"] or p.stem.lower() in ENTRY_NAMES or info["args"]:
                entries.append(info)
            py_sections.append(info)
        elif ext in CONFIG_EXTS:
            info = _summarize_config(p, rel)
            if info is None:
                continue
            if "skipped" in info:
                skipped_notes.append(f"- {rel}: {info['skipped']}")
            else:
                cfg_sections.append(info)

    out = list(header) + tree_lines + [""]
    if entries:
        out += ["## 入口脚本与命令行参数", ""]
        for info in entries:
            out.append(f"### {info['rel']}" + (f" — {info['doc']}" if info["doc"] else ""))
            for a in info["args"]:
                parts = [f"`{'/'.join(a['names'])}`"]
                if a["type"]:
                    parts.append(f"type={a['type']}")
                if a["default"] is not None:
                    parts.append(f"default={a['default']}")
                if a["choices"]:
                    parts.append(f"choices={a['choices']}")
                if a["help"]:
                    parts.append(f"help={a['help']}")
                out.append(f"- {info['rel']}:{a['line']}  " + ", ".join(parts))
            if info["has_main"] and not info["args"]:
                out.append(f"- {info['rel']}: 有 `if __name__ == \"__main__\"` 入口,无 argparse 参数")
            out.append("")
    if py_sections:
        out += ["## Python 模块(顶层类/函数)", ""]
        for info in py_sections:
            defs = info["defs"][:MAX_DEFS_PER_FILE]
            if not defs and not info["doc"]:
                continue
            out.append(f"### {info['rel']}" + (f" — {info['doc']}" if info["doc"] else ""))
            for line, name in defs:
                out.append(f"- {info['rel']}:{line}  {name}")
            if len(info["defs"]) > MAX_DEFS_PER_FILE:
                out.append(f"- …另有 {len(info['defs']) - MAX_DEFS_PER_FILE} 个定义未列出")
            out.append("")
    if cfg_sections:
        out += ["## 配置文件", ""]
        for info in cfg_sections:
            out.append(f"### {info['rel']}")
            for line_no, text in info["lines"]:
                loc = f"{info['rel']}:{line_no}" if line_no else info["rel"]
                out.append(f"- {loc}  {text}")
            if info["more"]:
                out.append(f"- …另有 {info['more']} 行未列出")
            out.append("")
    if skipped_notes:
        out += ["## 未能解析的文件", ""] + skipped_notes + [""]
    text = "\n".join(out)
    if len(text) > MAX_INDEX_CHARS:
        text = text[:MAX_INDEX_CHARS] + "\n\n[索引过长,已截断;完整代码在 inbox/code/]\n"
    return text + ("\n" if not text.endswith("\n") else "")
