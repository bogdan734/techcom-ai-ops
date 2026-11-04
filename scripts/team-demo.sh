#!/bin/sh
# Five days of the team layer on the demo fixtures: the layer starts, two tasks close, people
# answer and ask, a call goes unanswered, and running a day twice sends nothing twice.
# Ends with checks that fail the script if anything went out twice or a level leaked.
set -e
PYTHON=${PYTHON:-python3}
OUT=${OUT:-out/team}
ORG=fixtures/team/org.json
FOLLOWUP="env PYTHONPATH=src $PYTHON -m followup"
export CALLS=true CALL_TIME=10:00 AUDIT_RATE=0.25

rm -rf "$OUT"

day() { # DATE TASKS-FILE [extra run args]
  echo "\n=== $1: run"
  date=$1; tasks=$2; shift 2
  $FOLLOWUP run --org "$ORG" --tasks "fixtures/team/$tasks" --out "$OUT" --today "$date" "$@"
}
team() { # TODAY command... : act as a person on that day
  today=$1; shift
  $FOLLOWUP team "$@" --org "$ORG" --tasks fixtures/team/tasks-d2.json --out "$OUT" --today "$today"
}

day 2026-10-21 tasks-d1.json
day 2026-10-22 tasks-d2.json

echo "\n=== 2026-10-22: what John and Alex are asked"
team 2026-10-22 quiz TC-1001 --as john
team 2026-10-22 quiz TC-1005 --as alex

echo "\n=== 2026-10-22: Alex answers badly, Maria asks the assistant, Alex asks support"
team 2026-10-22 answer TC-1005 --as alex --all "not sure"
team 2026-10-22 chat TC-1011 "Which photos does the audit list?" --as maria
team 2026-10-22 support "I cannot open the quiz page" --as alex

day 2026-10-23 tasks-d2.json
echo "\n=== 2026-10-23: Maria reads the thread; Noah sees his line"
team 2026-10-23 chat TC-1011 --as maria
team 2026-10-23 report --as noah

day 2026-10-24 tasks-d2.json --fault call=no-answer
day 2026-10-25 tasks-d2.json

echo "\n=== 2026-10-25: the same day again must send nothing"
before=$(wc -l < "$OUT/delivery_log.jsonl")
day 2026-10-25 tasks-d2.json
after=$(wc -l < "$OUT/delivery_log.jsonl")

echo "\n=== checks"
total=$(wc -l < "$OUT/delivery_log.jsonl" | tr -d ' ')
unique=$(cut -d'"' -f8 "$OUT/delivery_log.jsonl" | sort -u | wc -l | tr -d ' ')
echo "messages $total, distinct idempotency keys $unique, calls $(wc -l < "$OUT/call_log.jsonl" | tr -d ' ')"
test "$before" -eq "$after"
test "$total" -eq "$unique"
test -f "$OUT/call_log.jsonl"
test ! -e "$OUT/state.db"
! grep -q 'tightened\|reduced\|halted' "$OUT/team.json"
echo "TEAM DEMO OK"
