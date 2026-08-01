#!/bin/bash
# Oracle: upstream ships no groundtruth_workspace for this task (the grader
# validates /app/NOTE.md with regex heuristics against the lecture content).
# The solution dir ships a hand-written NOTE.md, authored from the lecture
# slides (evaluation/code.md snippets + HW.pdf explanation), that satisfies
# every grader check. Verified locally against check_local.py.
set -e
cp /solution/NOTE.md /app/NOTE.md
