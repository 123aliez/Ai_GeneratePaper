"""Committed artifact manifest：每完成一个 stage/章，由平台校验后落地的产物清单。

引擎把产物写在 work/ 下（暂存区），平台「看到 stage 完成的日志 + 校验产物存在」之后，
才把对应文件 copy 到 output/ 并写进 manifest——这是 committed checkpoint。
崩溃后 resume 只读 output/ + manifest，不相信 work/ 里可能写了一半的半成品。
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .run_context import RunContext


@dataclass
class ArtifactRecord:
    artifact_type: str          # final | draft | review | evidence | plan | number_check | ...
    chapter: str                # 所属章节文件夹（整篇级用 "__paper__"）
    relative_path: str          # 相对 work/ 的路径（给前端展示）
    sha256: str
    size_bytes: int


@dataclass
class StageRecord:
    stage_name: str
    chapter: str
    status: str                 # completed | failed | skipped
    artifact_types: list[str] = field(default_factory=list)


@dataclass
class TaskManifest:
    task_id: str
    source_revision: int
    source_fingerprint: str
    stages: list[StageRecord] = field(default_factory=list)
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    completed_chapters: list[str] = field(default_factory=list)
    final_chapter_order: list[str] = field(default_factory=list)

    def manifest_path(self, ctx: RunContext) -> Path:
        return ctx.checkpoint_dir / "manifest.json"

    def save(self, ctx: RunContext) -> None:
        ctx.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path(ctx).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, ctx: RunContext) -> "TaskManifest | None":
        p = ctx.checkpoint_dir / "manifest.json"
        if not p.is_file():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        stages = [StageRecord(**s) for s in data.get("stages", [])]
        artifacts = [ArtifactRecord(**a) for a in data.get("artifacts", [])]
        return cls(
            task_id=data["task_id"],
            source_revision=data["source_revision"],
            source_fingerprint=data["source_fingerprint"],
            stages=stages,
            artifacts=artifacts,
            completed_chapters=data.get("completed_chapters", []),
            final_chapter_order=data.get("final_chapter_order", []),
        )

    def has_chapter(self, chapter: str) -> bool:
        return chapter in self.completed_chapters
