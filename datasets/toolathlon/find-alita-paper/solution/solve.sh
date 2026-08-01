#!/bin/bash
# Oracle for find-alita-paper: the grader looks for the downloaded paper as
# alita_<arxiv_id>*.pdf in the workspace (or arxiv_local_storage/) and checks
# it against the arXiv copy, so install the cached groundtruth PDF under the
# name the agent's download would have.
set -e
mkdir -p /app/arxiv_local_storage
for f in /solution/groundtruth_workspace/gt_alita_*.pdf; do
  cp "$f" "/app/$(basename "$f" | sed 's/^gt_//')"
done
ls -l /app/*.pdf
echo "oracle: paper PDF installed"
