#!/usr/bin/env bash
# Runs ON THE SERVER (called over SSH by the deploy workflow): applies migrations, switches to the new image, waits for
# it to be healthy, and goes back to the previous image if it is not.
#   usage: remote-deploy.sh ghcr.io/<owner>/<repo>:<sha>
set -euo pipefail

NEW_IMAGE="${1:?usage: remote-deploy.sh <image>}"
cd /opt/gropto
COMPOSE=(docker compose -f docker-compose.prod.yml)
PREVIOUS_IMAGE="$(cat .current_image 2>/dev/null || true)"

echo "==> Pulling $NEW_IMAGE"
docker pull "$NEW_IMAGE"

# A dump of the Supabase database before the schema changes. Best effort: a failed dump (for example a pg_dump older than
# the Supabase server) is reported but does not stop the deploy, because Supabase keeps its own backups too.
echo "==> Backing up the database before migrating"
mkdir -p backups && chown 10001 backups 2>/dev/null || true
if ! IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" run --rm backup; then
  echo "!! Backup did not complete - continuing; check the Supabase dashboard backups (Project Settings -> Database -> Backups)."
fi

echo "==> Applying database migrations"
IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" run --rm migrate

echo "==> Starting the new version"
IMAGE="$NEW_IMAGE" "${COMPOSE[@]}" up -d --remove-orphans api worker caddy

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
    IMAGE="$PREVIOUS_IMAGE" "${COMPOSE[@]}" up -d --remove-orphans api worker caddy
  fi
  exit 1
fi

echo "$NEW_IMAGE" > .current_image
docker image prune -f >/dev/null
echo "==> Deployed $NEW_IMAGE"
