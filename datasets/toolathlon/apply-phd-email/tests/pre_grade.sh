#!/bin/bash
# Upstream ships the reference Application_Materials tree compressed
# (files.tar.gz). The grader looks for an Application_Materials* directory
# directly under groundtruth_workspace, so unpack it verifier-side. Pure
# decompression of the shipped fixture — no derivation.
set -e
GT=/tests/pkg/tasks/finalpool/apply-phd-email/groundtruth_workspace
if ! ls "$GT"/Application_Materials* >/dev/null 2>&1; then
  tar xzf "$GT/files.tar.gz" -C "$GT"
fi
