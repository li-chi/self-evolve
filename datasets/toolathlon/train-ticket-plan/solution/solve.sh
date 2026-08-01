#!/bin/bash
# Oracle for train-ticket-plan.
#
# This task is graded against LIVE 12306 data: the grader launches the
# rail_12306 MCP server itself and re-queries the timetable for the dates
# derived from the run's launch time, then checks that the trains the agent
# named actually exist and satisfy the constraints. A reference answer
# therefore cannot be stored — it only exists relative to the timetable at
# grading time, and the valid combination changes run to run.
#
# The honest oracle is "solve it the same way the agent must", which needs
# the same live search the agent performs. Not implemented: this task's
# validation bar is nop -> 0.0 plus a grader that reaches live 12306 and
# reports a sensible mismatch rather than crashing (see COVERAGE.md).
echo "oracle: n/a for this live-data task — see the comment in solve.sh"
exit 1
