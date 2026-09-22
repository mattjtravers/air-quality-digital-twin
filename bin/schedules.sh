#!/usr/bin/env bash
# The maintenance switch — @spec INFRA-OPS-006
#
# Scheduled runs are turned off at their source, by disabling the EventBridge schedules. Nothing
# is dispatched, so no workflow run is created and there is nothing to skip. Manual
# workflow_dispatch runs are unaffected and keep working while the schedules are off.
#
# Usage: bin/schedules.sh {on|off|status}
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
SCHEDULES=(
    aqdt-ingest-purpleair
    aqdt-ingest-airnow
    aqdt-calibrate-fit
    aqdt-calibrate-apply
)

usage() {
    echo "usage: $(basename "$0") {on|off|status}" >&2
    exit 64
}

[ $# -eq 1 ] || usage

# update-schedule replaces the whole schedule, so every unchanged field has to be sent back with
# it. Reading the current definition and editing one key is what keeps the cron expressions,
# retry policy and target intact.
set_state() {
    local name="$1" state="$2" current
    current=$(aws scheduler get-schedule --name "$name" --region "$REGION" --no-cli-pager)

    python3 -c '
import json, subprocess, sys
name, state, region, current = sys.argv[1:]
schedule = json.loads(current)
for key in ("Arn", "CreationDate", "LastModificationDate"):
    schedule.pop(key, None)
schedule["State"] = state
subprocess.run(
    ["aws", "scheduler", "update-schedule", "--region", region, "--no-cli-pager",
     "--cli-input-json", json.dumps(schedule)],
    check=True, stdout=subprocess.DEVNULL,
)
' "$name" "$state" "$REGION" "$current"

    echo "$name -> $state"
}

case "$1" in
    on)
        for name in "${SCHEDULES[@]}"; do set_state "$name" ENABLED; done
        ;;
    off)
        for name in "${SCHEDULES[@]}"; do set_state "$name" DISABLED; done
        ;;
    status)
        for name in "${SCHEDULES[@]}"; do
            state=$(aws scheduler get-schedule --name "$name" --region "$REGION" \
                --query State --output text --no-cli-pager 2>/dev/null || echo "ABSENT")
            printf '%-28s %s\n' "$name" "$state"
        done
        ;;
    *)
        usage
        ;;
esac
