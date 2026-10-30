#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
SUFFIX=$(docker run --rm --network none python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb python -c 'import secrets;print(secrets.token_hex(5))')
PROJECT="mprp_verify_$SUFFIX"
case "$PROJECT" in mprp_verify_*) ;; *) exit 2 ;; esac
RECORDS=${MPRP_EVIDENCE_DIR:-"$(dirname "$ROOT")/MPRP Implementation Records/$PROJECT"}
mkdir -p "$RECORDS"
RECORDS=$(CDPATH= cd -- "$RECORDS" && pwd -P)
CONFIG="$RECORDS/environment"
. "$ROOT/scripts/compose.sh"
compose() { mprp_compose -f "$ROOT/compose.verify.yaml" "$@"; }
case "$RECORDS" in "$ROOT"|"$ROOT"/*) echo 'Verification evidence must be outside source' >&2;exit 2 ;; esac
if [ -e "$CONFIG" ] || [ -L "$CONFIG" ]; then echo 'Refusing to overwrite an existing evidence configuration' >&2;exit 2;fi
EXISTING=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")
EXISTING_CONTAINERS=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
EXISTING_NETWORKS=$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT")
if [ -n "$EXISTING$EXISTING_CONTAINERS$EXISTING_NETWORKS" ]; then echo 'Refusing to reuse existing verification resources' >&2;exit 2;fi
CONFIG_OWNED=0
cleanup() {
  compose logs --no-color --tail 200 web worker simulator simulator_worker > "$RECORDS/service.log" 2>&1 || true
  compose down --volumes --remove-orphans --timeout 20 > "$RECORDS/cleanup.log" 2>&1 || true
  if [ "$CONFIG_OWNED" = 1 ]; then rm -f "$CONFIG";fi
}
RUNNER_PID=$$
(
  sleep 2400
  printf 'Verification exceeded its 40 minute orchestration deadline.\n' > "$RECORDS/timeout.txt"
  kill -TERM "$RUNNER_PID"
) &
WATCHDOG_PID=$!
finish() {
  kill "$WATCHDOG_PID" 2>/dev/null || true
  cleanup
}
trap finish EXIT
trap 'exit 124' INT TERM
docker build --target runtime -t "$PROJECT:app" . > "$RECORDS/build.log" 2>&1
docker image inspect "$PROJECT:app" --format '{{.Id}}' > "$RECORDS/runtime-image.txt"
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$RECORDS:/output" "$PROJECT:app" python scripts/bootstrap_local.py --output /output --project "$PROJECT"
CONFIG_OWNED=1
# Ephemeral loopback host ports; resource names derive only from this generated project.
printf '\nWEB_PORT=0\nMAIL_PORT=0\n' >> "$CONFIG"
compose config --quiet
compose up -d --wait --wait-timeout 120 db simulator_db mailpit
compose run --rm role_setup
compose run --rm migrate
compose run --rm simulator_migrate
compose up -d simulator simulator_worker
compose run --rm web python scripts/run_bounded.py 120 python manage.py prepare_run
compose up -d --wait --wait-timeout 120 web worker
compose build tests > "$RECORDS/browser-build.log" 2>&1
docker image inspect "$PROJECT:verify" --format '{{.Id}}' > "$RECORDS/verification-image.txt"
compose run --rm tests python manage.py verify_contract > "$RECORDS/schema-check.log" 2>&1
compose run --rm simulator_tests python manage.py makemigrations --check --dry-run --settings=config.simulator_settings > "$RECORDS/simulator-schema-check.log" 2>&1
docker inspect "$(compose ps -q web)" --format '{{json .Mounts}}' > "$RECORDS/web-mounts.json"
compose run --rm tests python scripts/run_bounded.py 360 python manage.py test accounts merchants payments accounting settlements reconciliation disputes operations tests.test_exports --noinput --keepdb --verbosity=2 > "$RECORDS/main-tests.log" 2>&1
compose run --rm simulator_tests python scripts/run_bounded.py 180 python manage.py test processor_sim --noinput --keepdb --verbosity=2 --settings=config.simulator_settings > "$RECORDS/simulator-tests.log" 2>&1
compose run --rm journey python scripts/run_bounded.py 240 python tests/integration/journey.py > "$RECORDS/journey.log" 2>&1
mkdir -p "$RECORDS/browser"
chmod 777 "$RECORDS/browser"
: > "$RECORDS/browser.log"
for engine in chromium firefox webkit; do
  compose run --rm -v "$RECORDS/browser:/evidence" browser python scripts/run_bounded.py 300 python tests/browser/smoke.py --engine "$engine" >> "$RECORDS/browser.log" 2>&1
done
compose run --rm journey python tests/integration/restart_check.py > "$RECORDS/before-restart.json" 2> "$RECORDS/before-restart.log"
compose stop web worker
compose run --rm web python scripts/run_bounded.py 120 python manage.py prepare_run --new-run
compose up -d --wait --wait-timeout 120 web worker
compose run --rm journey python tests/integration/restart_check.py > "$RECORDS/after-restart.json" 2> "$RECORDS/after-restart.log"
cmp "$RECORDS/before-restart.json" "$RECORDS/after-restart.json"
compose run --rm web python manage.py verify_capabilities --require-all > "$RECORDS/health.log" 2>&1
compose run --rm tests python -m pip_audit --disable-pip --no-deps -r requirements.lock --format json > "$RECORDS/dependency-audit.json" 2>&1
compose run --rm tests python -m pip_audit --disable-pip --no-deps -r requirements-dev.lock --format json > "$RECORDS/development-dependency-audit.json" 2>&1
printf 'PASS: isolated verification %s\nEvidence: %s\n' "$PROJECT" "$RECORDS"
