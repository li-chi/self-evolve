#!/usr/bin/env python3
"""Fold a harbor job's rollouts into the curated archive layout:

    jobs/toolathlon/<task-name>/<model>-#<n>/

Only complete rollouts move (result.json + agent/trajectory.json +
verifier/reward.txt); incomplete trials are left in place and reported.
Numbering continues from the highest existing #n per task, ordered by
trial start time.

    python3 tools/archive_rollouts.py jobs/<job-name> [--model qwen3.6-35b]
    python3 tools/archive_rollouts.py jobs/<job-name> --dry-run
"""

import argparse
import glob
import json
import os
import re
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(REPO, "jobs", "toolathlon")


def complete(trial):
    return all(os.path.exists(os.path.join(trial, p)) for p in
               ("result.json", "agent/trajectory.json",
                "verifier/reward.txt"))


def next_index(task_dir, model):
    n = 0
    for d in glob.glob(os.path.join(task_dir, f"{model}-#*")):
        m = re.search(r"#(\d+)$", d)
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job_dir")
    ap.add_argument("--model", default="qwen3.6-35b")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    trials = []
    for rj in glob.glob(os.path.join(args.job_dir, "*", "result.json")):
        trial = os.path.dirname(rj)
        r = json.load(open(rj))
        task = (r.get("task_name") or "").split("/")[-1]
        if not task:
            continue
        if not complete(trial):
            print(f"skip (incomplete): {trial}")
            continue
        trials.append((task, r.get("started_at") or "", trial))

    moved = 0
    for task, _, trial in sorted(trials):
        task_dir = os.path.join(ARCHIVE, task)
        dest = os.path.join(task_dir, f"{args.model}-#{next_index(task_dir, args.model)}")
        print(f"{trial} -> {os.path.relpath(dest, REPO)}")
        if not args.dry_run:
            os.makedirs(task_dir, exist_ok=True)
            shutil.move(trial, dest)
        moved += 1
    print(f"{'would move' if args.dry_run else 'moved'} {moved} rollouts")


if __name__ == "__main__":
    main()
