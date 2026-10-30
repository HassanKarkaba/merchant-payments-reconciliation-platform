#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
CONFIG=${MPRP_CONFIG:-.local/environment}
PROJECT=
. "$ROOT/scripts/compose.sh"
compose() { mprp_compose "$@"; }
MAIN_DUMP=${1:-}
SIM_DUMP=${2:-}
if [ ! -f "$MAIN_DUMP" ] || [ ! -f "$SIM_DUMP" ]; then
  echo 'Provide the main and independently retained simulator .dump paths from one coordinated backup.' >&2;exit 2
fi
ENVIRONMENT_ID=$(sed -n 's/^MPRP_ENVIRONMENT_ID=//p' "$CONFIG")
printf 'This replaces both databases in this local project. Type environment %s: ' "$ENVIRONMENT_ID"
read -r CONFIRMATION
if [ "$CONFIRMATION" != "$ENVIRONMENT_ID" ]; then echo 'Selector did not match; nothing changed.' >&2;exit 2;fi
compose stop web worker simulator_worker simulator
compose exec -T db pg_restore -U postgres --clean --if-exists --exit-on-error -d mprp < "$MAIN_DUMP"
compose exec -T simulator_db pg_restore -U postgres --clean --if-exists --exit-on-error -d simulator < "$SIM_DUMP"
compose run --rm migrate
compose run --rm simulator_migrate
compose run --rm web python manage.py restore_hold --operator local-restore --reason 'Explicit coordinated database restoration; inventory and distinct finance release required'
compose run --rm simulator python manage.py mark_simulator_restored --operator local-restore --reason 'Explicit simulator database restoration' --settings=config.simulator_settings
compose up -d simulator simulator_worker
compose run --rm web python manage.py prepare_run --new-run
compose up -d web worker
echo 'Restored databases remain on financial hold. Run recovery_inventory and separate finance approve_restore_release.'
