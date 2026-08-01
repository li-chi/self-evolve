#!/bin/bash
# Oracle: the grader loads /app/Account_Book.xlsx and requires 126 rows,
# rows 1-121 matching the reference ledger and the last 5 rows matching one
# of two accepted orderings. The completed groundtruth ledger satisfies both.
set -e
cp /solution/groundtruth_workspace/Account_Book_Complete.xlsx /app/Account_Book.xlsx
