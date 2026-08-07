"""
Experiment-Paper Multi-Agent Writing Framework — Main Entry Point

Content source is data/ (experiment results) when PAPER_MODE=experiment, or
references/ notes when PAPER_MODE=survey. Section workspaces live under workspace/.

Usage:
    python run.py my-paper              # Manager Agent orchestration (default)
    python run.py my-paper --progress   # Python-controlled progress mode (recommended)
    python run.py my-paper --direct     # Direct Python orchestration (no Manager)
    python run.py my-paper --stream-raw # Manager raw event stream (debug only)
    python run.py --list                # List workspace folders that have a brief.md

A workspace folder needs a brief.md (see workspace/_TEMPLATE/brief.md). In
experiment mode, drop results into data/ first (see data/README.md).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import PAPER_ROOT, PAPER_MODE, get_draft_model, get_review_model, get_manager_model
from agents import create_agents, run_4stage_via_manager, run_4stage_via_manager_stream, run_4stage_direct, run_4stage_with_progress


def list_paper_folders():
    """List all paper folders that contain a brief.md."""
    print(f"Paper root: {PAPER_ROOT}\n")
    for name in sorted(os.listdir(PAPER_ROOT)):
        folder = os.path.join(PAPER_ROOT, name)
        if not os.path.isdir(folder):
            continue
        has_brief = os.path.exists(os.path.join(folder, "brief.md"))
        status = "ready" if has_brief else "no brief.md"
        print(f"  [{status:12s}] {name}")


def main():
    if "--list" in sys.argv:
        list_paper_folders()
        return

    if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
        print(__doc__)
        print("Available folders:")
        list_paper_folders()
        return

    folder_name = sys.argv[1]
    use_direct = "--direct" in sys.argv
    use_stream_raw = "--stream-raw" in sys.argv
    use_progress = "--progress" in sys.argv
    use_stream = use_stream_raw  # --stream 已废弃，只保留 --stream-raw 调试

    if sum([use_direct, use_stream_raw, use_progress]) > 1:
        print("Error: --direct, --progress, --stream-raw are mutually exclusive.")
        sys.exit(1)

    # C1: in experiment mode, only --progress runs the full mode-aware pipeline
    # (evidence mining, content source, number gate, convergence loop). The
    # Manager/direct/stream-raw paths are survey-only and would read the wrong
    # files and bypass every experiment safeguard — refuse them explicitly.
    if PAPER_MODE == "experiment" and not use_progress:
        print("Error: experiment mode requires --progress.")
        print("The --direct / --stream-raw / default-Manager paths are survey-only and")
        print("would skip the content source, number gate, and convergence loop.")
        print("Run: python run.py \"<workspace>\" --progress")
        sys.exit(1)

    folder_path = os.path.join(PAPER_ROOT, folder_name)

    if not os.path.isdir(folder_path):
        print(f"Error: folder '{folder_path}' does not exist.")
        sys.exit(1)

    brief_path = os.path.join(folder_path, "brief.md")
    if not os.path.exists(brief_path):
        print(f"Error: {brief_path} not found. Create brief.md first.")
        sys.exit(1)

    print(f"Initializing models...")
    model_draft = get_draft_model()
    model_review = get_review_model()
    model_manager = get_manager_model()

    print(f"Creating agents...")
    manager, draft_agent, review_agent = create_agents(model_draft, model_review, model_manager)

    mode = "direct" if use_direct else "progress" if use_progress else "stream-raw" if use_stream_raw else "manager"
    print(f"\n{'='*60}")
    print(f"Running 4-stage iteration on: {folder_name}")
    print(f"Mode: {mode}")
    print(f"{'='*60}")

    if use_progress:
        results = run_4stage_with_progress(draft_agent, review_agent, folder_path, manager)
        print(f"\nStages completed: {len(results)}")
    elif use_direct:
        results = run_4stage_direct(draft_agent, review_agent, folder_path)
        print(f"\nStages completed: {len(results)}")
    elif use_stream_raw:
        events = run_4stage_via_manager_stream(manager, folder_path, raw=True)
        print(f"\nStream events received: {len(events)}")
    else:
        result = run_4stage_via_manager(manager, folder_path)
        print(f"\nManager result:\n{str(result)[:500]}")


if __name__ == "__main__":
    main()
