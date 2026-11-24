"""整章编辑的离线回归：覆盖无缺陷、缺陷收敛、断点续跑及损坏清单退路。"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import orchestrator as orch


class EditorialRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.calls = []
        self.recorded = []
        self.issue = {"id": "MF1", "location": "Results paragraphs 2-4",
                      "issue": "Table recitation replaces the required comparison.",
                      "suggestion": "Merge into one finding and retain its material exception."}
        for name in ("brief.md", "input.md", "draft-v1.md", "review-v1.md"):
            (self.folder / name).write_text(name, encoding="utf-8")

    def write_review(self, must_fix):
        (self.folder / "review-v1.json").write_text(json.dumps({
            "must_fix": must_fix,
            "should_fix": [{"id": "SF1", "location": "Results paragraph 5",
                            "issue": "The shared protocol is defined again.",
                            "suggestion": "Keep the definition only in Setup."}],
        }), encoding="utf-8")

    def run_loop(self, resolved_after=1):
        def stage(agent, role, prompt, docs, outputs):
            self.calls.append((role, prompt, [d.name for d in docs], outputs))
            for name in outputs:
                if name.startswith("review-verify.round-"):
                    number = int(name.split("round-")[1].split(".")[0])
                    resolved = number >= resolved_after
                    value = json.dumps({"all_resolved": resolved, "items": [
                        {"id": "MF1", "resolved": resolved, "note": "fixture verdict"}]})
                else:
                    value = f"Revised artifact: {name}"
                (self.folder / name).write_text(value, encoding="utf-8")
            return "ok"

        with patch.object(orch, "idea_clause", return_value=""), contextlib.redirect_stdout(io.StringIO()):
            return orch.run_convergence_loop(
                None, None, str(self.folder), self.folder.name, "", "", stage,
                lambda names: all((self.folder / n).is_file() for n in names),
                record_stage=lambda names, kind: self.recorded.append((names, kind)))

    def test_no_must_fix_still_edits_whole_chapter(self):
        self.write_review([])
        self.run_loop()
        self.assertEqual(len(self.calls), 1)
        role, prompt, docs, _ = self.calls[0]
        self.assertEqual(role, "Draft")
        self.assertIn("WHOLE-CHAPTER EDIT", prompt)
        self.assertIn("SHOULD FIX", prompt)
        self.assertIn("review-v1.md", docs)
        self.assertTrue((self.folder / "draft-v2.md").exists())

    def test_first_edit_then_bounded_repair_with_frozen_acceptance(self):
        self.write_review([self.issue])
        self.run_loop(resolved_after=2)
        drafts = [c for c in self.calls if c[0] == "Draft"]
        verifications = [c for c in self.calls if c[0] == "Review"]
        self.assertEqual(len(drafts), 2)
        self.assertIn("WHOLE-CHAPTER EDIT", drafts[0][1])
        self.assertNotIn("do not rewrite unaffected paragraphs", drafts[0][1])
        self.assertNotIn("WHOLE-CHAPTER EDIT", drafts[1][1])
        self.assertIn("Fix ONLY these open items", drafts[1][1])
        self.assertIn("draft-v2.round-1.md", drafts[1][2])
        for _, prompt, _, _ in verifications:
            self.assertIn("Do NOT raise new issues", prompt)
            self.assertIn(self.issue["issue"], prompt)
            self.assertNotIn("WHOLE-CHAPTER EDIT", prompt)
        self.assertEqual((self.folder / "draft-v2.md").read_text(),
                         (self.folder / "draft-v2.round-2.md").read_text())
        self.assertEqual(orch.checklist_status(str(self.folder)), [])
        self.assertEqual(len(self.recorded), 4)

    def test_resume_keeps_completed_edit_and_only_repairs_open_item(self):
        self.write_review([self.issue])
        draft = self.folder / "draft-v2.round-1.md"
        draft.write_text("Already edited whole chapter", encoding="utf-8")
        (self.folder / "review-verify.round-1.json").write_text(json.dumps({
            "all_resolved": False, "items": [{"id": "MF1", "resolved": False}]
        }), encoding="utf-8")
        self.run_loop(resolved_after=2)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all("WHOLE-CHAPTER EDIT" not in c[1] for c in self.calls))
        self.assertEqual(draft.read_text(), "Already edited whole chapter")
        self.assertEqual(orch.checklist_status(str(self.folder)), [])

    def test_unresolved_editorial_defect_does_not_pass_verification(self):
        self.write_review([self.issue])
        with patch.object(orch, "MAX_REVISION_ROUNDS", 2):
            self.run_loop(resolved_after=3)
        self.assertTrue(orch.checklist_status(str(self.folder)))
        self.assertIn("MF1", (self.folder / "todo.md").read_text())

    def test_malformed_review_fallback_keeps_edit_but_not_verified_status(self):
        (self.folder / "review-v1.json").write_text("{broken", encoding="utf-8")
        self.run_loop()
        self.assertEqual(len(self.calls), 1)
        self.assertIn("WHOLE-CHAPTER EDIT", self.calls[0][1])
        self.assertTrue(orch.checklist_status(str(self.folder)))


if __name__ == "__main__":
    unittest.main()
