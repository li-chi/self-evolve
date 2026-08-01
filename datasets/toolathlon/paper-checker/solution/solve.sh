#!/bin/bash
# Oracle: the grader (evaluation/main.py) extracts the groundtruth
# files.tar.gz and compares every .tex/.bib file under /app/my_paper
# line-by-line (normalize_str) against the groundtruth my_paper.
# Replace the buggy initial my_paper with the corrected groundtruth copy.
set -e
rm -rf /app/my_paper
tar xzf /solution/groundtruth_workspace/files.tar.gz -C /app my_paper
