#!/bin/bash
# Oracle: the grader (evaluation/check_local.py) compares whitespace/case
# normalized content of /app/survey.tex against the groundtruth survey.tex.
set -e
cp /solution/groundtruth_workspace/survey.tex /app/survey.tex
