#!/bin/bash
# Rebuild the grader's woocommerce_config.json (credentials + product
# mapping) from the store's own product records — see
# tests/derive_wc_config.py for why it is not carried over from preprocess.
set -e
/opt/toolathlon/.venv/bin/python /tests/derive_wc_config.py
