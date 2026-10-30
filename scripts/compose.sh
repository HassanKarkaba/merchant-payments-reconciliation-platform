# Shared explicit scope for every local and verification Docker operation.
# Sourced by wrappers after ROOT, CONFIG and (optionally) PROJECT are selected.
mprp_compose() (
  scope=${PROJECT:-$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' "$CONFIG")}
  case "$scope" in ''|-*|*[!a-z0-9_-]*) echo 'Invalid explicit Compose project identity' >&2;exit 2 ;; esac
  # Ambient exports cannot redirect generated project resources, image names,
  # credentials, environment identities, or loopback bindings.
  unset COMPOSE_PROJECT_NAME MPRP_ENVIRONMENT_ID DJANGO_SECRET_KEY FACTOR_ENCRYPTION_KEY SIMULATOR_API_KEY CALLBACK_HMAC_KEY
  unset DB_ADMIN_PASSWORD DB_OWNER_PASSWORD DB_RUNTIME_PASSWORD DB_PURGE_PASSWORD
  unset SIM_DB_ADMIN_PASSWORD SIM_DB_OWNER_PASSWORD SIM_DB_RUNTIME_PASSWORD SIM_DB_PURGE_PASSWORD WEB_PORT MAIL_PORT
  COMPOSE_PROJECT_NAME="$scope" docker compose -p "$scope" --env-file "$CONFIG" -f "$ROOT/compose.yaml" "$@"
)
