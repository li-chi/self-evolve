#!/bin/bash
# Rebuild the grader's expected_results.json from the store's own order
# history — see tests/derive_expected.py for why it is not carried over
# from preprocess (upstream's orders are randomised per run).
set -e
/opt/toolathlon/.venv/bin/python /tests/derive_expected.py
