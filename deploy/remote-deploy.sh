#!/usr/bin/env bash
# Runs ON THE SERVER (called over SSH by the deploy workflow): switches to the new image, waits for it to be healthy,
# and goes back to the previous image if it is not. Database migrations run by themselves when the new version has any the
# database does not have yet (RUN_MIGRATIONS: auto = only then, the default; true = always; false = never).
#   usage: remote-deploy.sh ghcr.io/<owner>/<repo>:<sha>
set -euo pipefail

NEW_IMAGE="${1:?usage: remote-deploy.sh <image>}"
cd /opt/gropto
COMPOSE=(docker compose -f docker-compose.prod.yml)
PREVIOUS_IMAGE="$(cat .current_image 2>/dev/null || true)"

echo "==> Pulling $NEW_IMAGE"
docker pull "$NEW_IMAGE"

# Migrations: by default they run on their own, and only when the database is behind this version (so an ordinary deploy with no
# schema change does nothing here). They run BEFORE the new version starts, so they are written to be additive and the version
# still running keeps working against the new schema. If a migration fails, the script stops here, nothing is switched, and the
# current version keeps serving.
need_migrations=false
case "${RUN_MIGRATIONS:-auto}" in
  true) need_migrations=true ;;
  false) need_migrations=false ;;
  *)
    if IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" run --rm migrate python -m alembic current --check-heads >/dev/null 2>&1; then
      need_migrations=false
    else
      need_migrations=true  # behind the new version (or the database could not be read: the migration step then says why)
    fi
    ;;
esac

if [ "$need_migrations" = "true" ]; then
  # A dump first. Best effort: a failed dump (for example a pg_dump older than the Supabase server) does not stop the deploy.
  echo "==> Backing up the database before migrating"
  mkdir -p backups && chown 10001 backups 2>/dev/null || true
  if ! IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" run --rm backup; then
    echo "!! Backup did not complete - continuing; Supabase keeps its own backups."
  fi
  echo "==> Applying database migrations"
  IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" run --rm migrate
else
  echo "==> No database migration needed (the database is already at this version)"
fi

# The background worker only runs when Redis is set up and notifications are not switched off: it polls Redis all day.
SERVICES="api caddy"
if grep -qE "^REDIS_URL=.+" .env 2>/dev/null && ! grep -qiE "^CELERY_ENABLED=false" .env 2>/dev/null; then
  SERVICES="api caddy worker"
else
  echo "==> Worker is off (no REDIS_URL, or CELERY_ENABLED=false)"
  "${COMPOSE[@]}" --profile worker rm -sf worker >/dev/null 2>&1 || true
fi

echo "==> Starting the new version"
IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" up -d --remove-orphans $SERVICES

echo "==> Waiting for the API to be healthy"
healthy=false
for _ in $(seq 1 40); do
  if IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" exec -T api curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 3
done

if [ "$healthy" != true ]; then
  echo "!! The new version did not become healthy. Recent API log:"
  IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" logs --tail=60 api || true
  if [ -n "$PREVIOUS_IMAGE" ]; then
    echo "==> Rolling back to $PREVIOUS_IMAGE"
    IMAGE="$PREVIOUS_IMAGE" "${COMPOSE[@]}" up -d --remove-orphans $SERVICES
  fi
  exit 1
fi

echo "$NEW_IMAGE" > .current_image
docker image prune -f >/dev/null
echo "==> Deployed $NEW_IMAGE"
