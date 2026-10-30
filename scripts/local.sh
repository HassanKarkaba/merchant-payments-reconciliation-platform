#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
CONFIG=${MPRP_CONFIG:-.local/environment}
PROJECT=
. "$ROOT/scripts/compose.sh"
compose() { mprp_compose "$@"; }
case "${1:-}" in
init)
  docker build --target runtime -t mprp-bootstrap .
  mkdir -p .local
  docker run --rm --network none --user "$(id -u):$(id -g)" -v "$ROOT/.local:/output" mprp-bootstrap python scripts/bootstrap_local.py --output /output
  compose config --quiet
  compose build web
  ;;
migrate)
  compose up -d --wait --wait-timeout 120 db simulator_db mailpit
  compose run --rm role_setup
  compose run --rm migrate
  compose run --rm simulator_migrate
  ;;
seed-demo)
  compose up -d simulator
  compose run --rm --user "$(id -u):$(id -g)" -v "$ROOT/.local:/output" web python manage.py seed_demo --output /output/demo-access.txt
  ;;
up)
  compose up -d simulator simulator_worker mailpit
  compose run --rm web python manage.py prepare_run
  compose up -d --wait --wait-timeout 120 web worker
  ;;
restart)
  compose stop web worker
  compose up -d simulator simulator_worker
  compose run --rm web python manage.py prepare_run --new-run
  compose up -d web worker
  ;;
health) compose run --rm web python manage.py verify_capabilities --require-all ;;
logs) compose logs --tail 100 web worker simulator simulator_worker ;;
stop) compose stop ;;
backup)
  mkdir -p .local/backups
  ENVIRONMENT_ID=$(sed -n 's/^MPRP_ENVIRONMENT_ID=//p' "$CONFIG")
  printf '%s\n' "$ENVIRONMENT_ID" > .local/backups/.mprp-backup-root
  chmod 600 .local/backups/.mprp-backup-root
  compose stop web worker simulator_worker simulator
  STAMP=$(date -u +%Y%m%dT%H%M%SZ)
  compose exec -T db pg_dump -U postgres -Fc mprp > ".local/backups/main-$STAMP.dump"
  compose exec -T simulator_db pg_dump -U postgres -Fc simulator > ".local/backups/simulator-$STAMP.dump"
  chmod 600 .local/backups/*.dump
  compose up -d simulator simulator_worker
  compose run --rm web python manage.py prepare_run --new-run
  compose up -d web worker
  ;;
restore)
  sh scripts/restore.sh "${2:-}" "${3:-}"
  ;;
*) echo 'Usage: sh scripts/local.sh init|migrate|seed-demo|up|restart|health|logs|stop|backup|restore MAIN_DUMP SIMULATOR_DUMP' >&2;exit 2 ;;
esac
