#!/bin/bash
# Oracle: the grader lists every .xlsx in /app and requires an exact 1:1
# permutation match against the 4 groundtruth option files. The task text
# says to delete the reference format file, so remove all pre-existing
# .xlsx before copying the groundtruth options in.
set -e
rm -f /app/*.xlsx
cp /solution/groundtruth_workspace/en_op1.xlsx \
   /solution/groundtruth_workspace/en_op2.xlsx \
   /solution/groundtruth_workspace/en_op3.xlsx \
   /solution/groundtruth_workspace/en_op4.xlsx \
   /app/
