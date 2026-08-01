#!/bin/bash
# Rebuild the grader's groundtruth from the store's order history — see
# tests/derive_expected.py for why it is not carried over from preprocess.
set -e
GT=/tests/pkg/tasks/finalpool/woocommerce-update-cover/groundtruth_workspace
mkdir -p "$GT"
/opt/toolathlon/.venv/bin/python /tests/derive_expected.py \
  "$GT/expected_results.json"
