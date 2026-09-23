#!/usr/bin/env bash
# =============================================================================
# rollback.sh - the same rollback logic as .github/workflows/rollback.yml, but
# for your terminal. Useful when GitHub is down, when you have no network access
# to the Actions UI, or when you simply want to show a senior what the workflow
# is actually doing underneath.
#
# LIKE THE WORKFLOW, THIS SCRIPT NEVER BUILDS AN IMAGE. It only points the ECS
# service at a task definition revision that already exists.
#
# Usage:
#   ./scripts/rollback.sh              # roll back to the revision saved in SSM
#   ./scripts/rollback.sh 5            # roll back to rollback-demo-task:5
#   ./scripts/rollback.sh rollback-demo-task:5
#
# Requirements: AWS CLI v2 and jq, with credentials that can read/update the
# service (run `aws sts get-caller-identity` first if you are unsure).
# =============================================================================

# set -e  : stop at the first command that fails
# set -u  : stop if an undefined variable is used (catches typos)
# set -o pipefail : a failure anywhere in a pipeline fails the whole pipeline
set -euo pipefail

# --- Configuration ------------------------------------------------------------
# Defaults match the fixed names used throughout this repo. Override any of them
# with an environment variable, e.g.  ECS_CLUSTER=other-cluster ./scripts/rollback.sh
AWS_REGION="${AWS_REGION:-eu-north-1}"
ECS_CLUSTER="${ECS_CLUSTER:-rollback-demo-cluster}"
ECS_SERVICE="${ECS_SERVICE:-rollback-demo-service}"
TASK_FAMILY="${TASK_FAMILY:-rollback-demo-task}"
SSM_PREVIOUS_PARAM="${SSM_PREVIOUS_PARAM:-/rollback-demo/prod/previous-taskdef}"

POLL_INTERVAL=15      # seconds between status checks
POLL_TIMEOUT=1200     # 20 minutes, matching the workflows

INPUT_REVISION="${1:-}"   # optional first argument

# Every AWS call reuses these flags.
AWS=(aws --region "$AWS_REGION")

# --- Small helpers ------------------------------------------------------------
log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Turn a full task definition ARN into the short "family:revision" form.
short_revision() { echo "${1##*/}"; }

# Which image does a given revision run? This is the proof that rollback is
# just a pointer change: the image tag it prints is an OLD tag, already in ECR.
image_of() {
  "${AWS[@]}" ecs describe-task-definition --task-definition "$1" \
    --query 'taskDefinition.containerDefinitions[0].image' --output text
}

# Poll until the service settles, printing progress so it never looks frozen.
wait_for_stable() {
  local deadline=$(( $(date +%s) + POLL_TIMEOUT ))
  local svc state count tasks event

  while true; do
    svc="$("${AWS[@]}" ecs describe-services \
      --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
      --query 'services[0]' --output json)"

    state="$(echo "$svc" | jq -r '[.deployments[] | select(.status=="PRIMARY") | .rolloutState // "IN_PROGRESS"][0] // "IN_PROGRESS"')"
    count="$(echo "$svc" | jq -r '.deployments | length')"
    tasks="$(echo "$svc" | jq -r '[.deployments[] | select(.status=="PRIMARY") | "\(.runningCount)/\(.desiredCount)"][0] // "?"')"
    event="$(echo "$svc" | jq -r '.events[0].message // "(no events yet)"')"

    log "rolloutState=${state} tasks=${tasks} deployments=${count} | ${event}"

    # Finished: new deployment completed and the old one has drained away.
    if [ "$state" = "COMPLETED" ] && [ "$count" -eq 1 ]; then
      return 0
    fi
    if [ "$state" = "FAILED" ]; then
      die "ECS reported rolloutState=FAILED. The target revision may itself be unhealthy - try an older one."
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      die "Timed out after ${POLL_TIMEOUT}s waiting for the service to stabilise."
    fi
    sleep "$POLL_INTERVAL"
  done
}

# --- 0. Sanity checks ---------------------------------------------------------
command -v aws >/dev/null 2>&1 || die "AWS CLI not found. Install AWS CLI v2 first."
command -v jq  >/dev/null 2>&1 || die "jq not found. Install jq first."

log "Region ${AWS_REGION} | cluster ${ECS_CLUSTER} | service ${ECS_SERVICE}"

# --- 1. What is running right now? -------------------------------------------
CURRENT_ARN="$("${AWS[@]}" ecs describe-services \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --query 'services[0].taskDefinition' --output text 2>/dev/null || echo "")"

[ -n "$CURRENT_ARN" ] && [ "$CURRENT_ARN" != "None" ] \
  || die "Could not read service ${ECS_SERVICE} in cluster ${ECS_CLUSTER}. Does it exist in ${AWS_REGION}?"

CURRENT_REV="$(short_revision "$CURRENT_ARN")"
CURRENT_IMAGE="$(image_of "$CURRENT_REV")"
log "Currently live: ${CURRENT_REV}  (${CURRENT_IMAGE})"

# --- 2. Is a deployment still in flight? --------------------------------------
# If ECS is mid-rollout, the cleanest undo is to have ECS stop and reverse it.
INFLIGHT_ARN="$("${AWS[@]}" ecs list-service-deployments \
  --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --status IN_PROGRESS PENDING \
  --query 'serviceDeployments[0].serviceDeploymentArn' \
  --output text 2>/dev/null || echo "None")"

if [ -n "$INFLIGHT_ARN" ] && [ "$INFLIGHT_ARN" != "None" ]; then
  log "A deployment is still in progress. Asking ECS to stop and roll it back..."
  "${AWS[@]}" ecs stop-service-deployment \
    --service-deployment-arn "$INFLIGHT_ARN" \
    --stop-type ROLLBACK >/dev/null

  wait_for_stable

  LIVE_REV="$(short_revision "$("${AWS[@]}" ecs describe-services \
    --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].taskDefinition' --output text)")"
  log "In-flight deployment rolled back. Service is running ${LIVE_REV} ($(image_of "$LIVE_REV"))."
  log "No image was built during this rollback."
  exit 0
fi

# --- 3. Resolve the rollback target -------------------------------------------
if [ -n "$INPUT_REVISION" ]; then
  # Accept "rollback-demo-task:5" or a bare "5".
  case "$INPUT_REVISION" in
    *:*) TARGET="$INPUT_REVISION" ;;
    *)   TARGET="${TASK_FAMILY}:${INPUT_REVISION}" ;;
  esac
  log "Target from argument: ${TARGET}"
else
  # The LAST KNOWN GOOD revision, not merely the previous one. The deploy
  # workflow only writes this pointer once a revision has completed its rollout,
  # run every task healthily and survived a soak period - so two deploys in
  # quick succession cannot leave it aiming at the bad release.
  TARGET="$("${AWS[@]}" ssm get-parameter --name "$SSM_PREVIOUS_PARAM" \
    --query 'Parameter.Value' --output text 2>/dev/null || echo "")"
  [ -n "$TARGET" ] && [ "$TARGET" != "None" ] \
    || die "No revision given and ${SSM_PREVIOUS_PARAM} is empty/missing. Deploy once first, or pass a revision: ./scripts/rollback.sh 3"
  log "Target from SSM, last known good (${SSM_PREVIOUS_PARAM}): ${TARGET}"

  # Show the alternatives, in case this target turns out to be bad as well.
  HISTORY="$("${AWS[@]}" ssm get-parameter --name "${SSM_PREVIOUS_PARAM%/*}/known-good-history" \
    --query 'Parameter.Value' --output text 2>/dev/null || echo '[]')"
  case "$HISTORY" in ''|None) HISTORY='[]' ;; esac
  if echo "$HISTORY" | jq -e 'type == "array" and length > 0' >/dev/null 2>&1; then
    echo
    echo "  Known-good history (pass one of these as an argument if needed):"
    echo "$HISTORY" | jq -r '.[] | "    \(.revision)  image \(.image | split(":") | last)  promoted \(.promoted_at)  soaked \(.soak_minutes)m"'
    echo
  fi
fi

# Never touch the service until we know the target really exists.
"${AWS[@]}" ecs describe-task-definition --task-definition "$TARGET" >/dev/null 2>&1 \
  || die "Task definition ${TARGET} does not exist. List them with: aws ecs list-task-definitions --family-prefix ${TASK_FAMILY} --region ${AWS_REGION}"

TARGET_IMAGE="$(image_of "$TARGET")"
log "Target image: ${TARGET_IMAGE}  (already in ECR - nothing to build)"

# Running this twice must be harmless.
if [ "$TARGET" = "$CURRENT_REV" ]; then
  log "Already running the rollback target - nothing to do."
  exit 0
fi

# --- 4. Switch the service over -----------------------------------------------
log "Rolling back: ${CURRENT_REV} -> ${TARGET}"
"${AWS[@]}" ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --task-definition "$TARGET" >/dev/null

wait_for_stable

# --- 5. Verify -----------------------------------------------------------------
LIVE_REV="$(short_revision "$("${AWS[@]}" ecs describe-services \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --query 'services[0].taskDefinition' --output text)")"

[ "$LIVE_REV" = "$TARGET" ] \
  || die "Expected ${TARGET} to be live but the service is running ${LIVE_REV}."

echo
echo "================ ROLLBACK COMPLETE ================"
echo "  from : ${CURRENT_REV}  (${CURRENT_IMAGE})"
echo "  to   : ${LIVE_REV}  (${TARGET_IMAGE})"
echo "  No image was built during this rollback."
echo "==================================================="

# NOTE: the SSM parameter is deliberately left untouched, exactly like the
# workflow, so that running this script twice is safe.
