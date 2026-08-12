"""真实网关在线烟测：建一个临时 work 目录（不动仓库的 idea/outline/data），
用真实 .env 的网关 + 真实 run.py 子进程跑通「--init → 单章 --progress」最小链路。

目标：验证平台 adapter（PAPER_RUNTIME_ROOT 注入 + 子进程跑 run.py + stdout 捕获）
在真实网关下端到端可用——这是「部署到服务器」前最关键的一跳。

做法：
  - 复制 idea.md / outline.md 到临时 work/
  - data/ 用 example（experiments/results 章会因 number gate advisory 而非阻塞）
  - 只跑第 1 章（Abstract，短章），reasoning 跟随 .env（Draft=max 会慢，可改 .env 临时调低）
  - 捕获子进程 stdout，确认有 [Manager] Stage 行 + 产出 final.md

不连 DB/Celery（纯 adapter 烟测），约 5-15 分钟（取决于网关与 reasoning 档）。
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.pipeline import (
    build_run_context, materialize_input_snapshot, materialize_workdir,
    SubprocessRunPyAdapter, compute_source_fingerprint,
)

PASSED = []
def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def smoke_real_gateway():
    tmp = Path(tempfile.mkdtemp(prefix="paper_smoke_"))
    print(f"[smoke] 临时 work 根: {tmp}", flush=True)

    # 1) 用仓库现有的 idea/outline/data 做输入快照（不污染仓库）
    inputs = {
        "idea": ROOT / "idea.md",
        "outline": ROOT / "outline.md",
        "data": ROOT / "data",
        "references": ROOT / "references",
    }
    fp = compute_source_fingerprint(inputs["idea"], inputs["outline"], inputs["data"])
    print(f"[smoke] source_fingerprint = {fp[:16]}...", flush=True)

    ctx = build_run_context(tmp, "smoke_user", "smoke_proj", "smoke_task", 1, fp)
    materialize_input_snapshot(ctx, inputs)
    materialize_workdir(ctx, skills_source=ROOT / "skills")

    check("work/idea.md 就位", (ctx.work_dir / "idea.md").is_file())
    check("work/outline.md 就位", (ctx.work_dir / "outline.md").is_file())
    check("work/data 就位", (ctx.work_dir / "data").is_dir())
    check("work/skills 链接", (ctx.work_dir / "skills" / "writing-style.md").is_file())

    # 2) adapter：先 --init 生成章节目录，再跑第 1 章
    adapter = SubprocessRunPyAdapter(repo_root=ROOT, python_executable=sys.executable)

    print(f"\n[smoke] Step 1: run.py --init（生成章节工作区）...", flush=True)
    t0 = time.monotonic()
    r_init = adapter.init_workspaces(ctx)
    print(f"[smoke] --init 完成: rc={r_init.returncode} 耗时 {time.monotonic()-t0:.1f}s", flush=True)
    check("--init rc=0", r_init.returncode == 0, f"rc={r_init.returncode}")

    ws = ctx.work_dir / "workspace"
    chapters = sorted([d.name for d in ws.iterdir() if d.is_dir() and not d.name.startswith("_")])
    print(f"[smoke] 生成的章节: {chapters}", flush=True)
    check("--init 生成了章节", len(chapters) > 0, f"chapters={chapters}")

    first_chapter = chapters[0]
    print(f"\n[smoke] Step 2: run.py {first_chapter} --progress（真实网关，单章）...",
          flush=True)
    print(f"[smoke]   reasoning 配置跟随 .env（Draft={os.getenv('DRAFT_REASONING_LEVEL','?')}）",
          flush=True)
    print(f"[smoke]   这一章可能要几分钟，stdout 实时落 {ctx.stdout_log.name}", flush=True)

    t0 = time.monotonic()
    events_seen: list[str] = []

    def on_event(ev: dict) -> None:
        events_seen.append(ev.get("event_type", "?"))
        # 只打关键事件，避免刷屏
        if ev.get("event_type") in ("STAGE_LINE", "FAILURE_LINE", "PIPELINE_COMPLETE"):
            elapsed = time.monotonic() - t0
            print(f"[smoke]   {elapsed:6.1f}s | {ev.get('message','')[:90]}", flush=True)

    r_chapter = adapter.run_chapter(ctx, first_chapter,
                                    event_callback=on_event, cancel_check=None)
    elapsed = time.monotonic() - t0
    print(f"\n[smoke] 单章完成: rc={r_chapter.returncode} 耗时 {elapsed:.1f}s "
          f"cancelled={r_chapter.cancelled}", flush=True)

    final = ctx.work_dir / "workspace" / first_chapter / "final.md"
    check("单章产出 final.md", final.is_file(),
          f"final 存在? {final.is_file()}")

    if final.is_file():
        txt = final.read_text(encoding="utf-8", errors="replace")
        print(f"[smoke] final.md 字数: {len(txt)} 字符, 前 200 字:", flush=True)
        print("  " + txt[:200].replace("\n", "\n  "), flush=True)
        check("final.md 非空", len(txt.strip()) > 100, f"len={len(txt)}")

    print(f"\n[smoke] 捕获到的事件类型: {sorted(set(events_seen))}", flush=True)
    check("捕获到 STAGE 事件", any("STAGE" in e for e in events_seen),
          f"events={events_seen[:10]}")

    print(f"\n[smoke] 原始 stdout 日志: {ctx.stdout_log}", flush=True)
    print(f"[smoke] 原始 stderr 日志: {ctx.stderr_log}", flush=True)


if __name__ == "__main__":
    smoke_real_gateway()
    print(f"\n全部通过：{len(PASSED)} 项检查（真实网关在线烟测）")
