# Deploying the backend (GitHub Actions -> Hostinger VPS)

Every push to `main` runs the tests, builds the Docker image, pushes it to GitHub Container Registry and deploys it to the
VPS. Pull requests run the tests and a Docker build only. The pipeline is `.github/workflows/ci-cd.yml`.

> This needs a Hostinger **VPS** (Docker has to run on it). Hostinger shared/web hosting cannot run this app.

## What runs on the server

`/opt/gropto` holds `docker-compose.prod.yml`, `Caddyfile`, `remote-deploy.sh` and `.env`, all written by the pipeline.

| Container | Job |
|---|---|
| `api` | the FastAPI app (gunicorn), reachable only through Caddy |
| `worker` | the Celery worker for notifications |
| `caddy` | public ports 80 and 443, HTTPS certificate handled for you |

Postgres (Supabase) and Redis (Upstash) stay managed services; their addresses come from `.env`.

## One-time setup

**1. Server.** On a fresh Ubuntu VPS, as root (installs Docker, opens only ports 22, 80 and 443):

```bash
bash deploy/server-setup.sh
```

**2. Deploy key.** On your computer make a key used only by GitHub:

```bash
ssh-keygen -t ed25519 -f gropto_deploy -C github-actions -N ""
ssh-copy-id -i gropto_deploy.pub root@<server-ip>     # or paste gropto_deploy.pub into /root/.ssh/authorized_keys
```

**3. GitHub secrets.** Repo -> Settings -> Secrets and variables -> Actions -> New repository secret:

| Secret | Value |
|---|---|
| `HOSTINGER_HOST` | the VPS IP address |
| `HOSTINGER_USER` | `root` (or the deploy user you created) |
| `HOSTINGER_SSH_KEY` | the full contents of the private file `gropto_deploy` |
| `HOSTINGER_PORT` | only if SSH is not on port 22 |
| `PROD_ENV` | the whole production `.env`, pasted as text (see below) |

Until `HOSTINGER_HOST`, `HOSTINGER_USER`, `HOSTINGER_SSH_KEY` and `PROD_ENV` exist, the deploy job is skipped with a notice
and nothing breaks.

**4. `PROD_ENV` contents.** Copy `.env.example` and fill it in. At least:

```
DATABASE_URL=postgresql+asyncpg://...      # Supabase pooler connection string (see "Supabase" below)
MIGRATION_DATABASE_URL=postgresql://...    # Supabase session pooler (port 5432), used for migrations and backups
JWT_SECRET=<long random string>            # python -c "import secrets; print(secrets.token_urlsafe(48))"
ENVIRONMENT=production
CORS_ORIGINS=app://.,https://your-domain
REDIS_URL=rediss://...                     # Upstash
SITE_ADDRESS=api.your-domain.com           # leave empty to serve plain HTTP on the IP for now
```

Point the domain's DNS `A` record at the server before setting `SITE_ADDRESS`, so Caddy can get the certificate. Add the
payment, WhatsApp, SMS and email keys when you use them.

**5. First deploy.** Push to `main`, or Actions -> CI/CD -> Run workflow.

Then check it: `curl https://<your-domain>/health/ready`

## Supabase

The database is a Supabase project, so no database runs on the server and CI never touches it (CI proves the migrations on
a throwaway Postgres; only the deploy job applies them to Supabase).

- Copy the connection strings from the Supabase dashboard: **Connect** -> **Session pooler** (port 5432) or **Transaction
  pooler** (port 6543). Use the **pooler** strings, not "Direct connection": the direct one needs IPv6, which many VPSs lack.
- In `DATABASE_URL` the scheme must be `postgresql+asyncpg://`.
- Session pooler caps at 15 connections, so keep `WEB_CONCURRENCY=1` (the default). With the transaction pooler (6543) more
  workers are fine, but then `MIGRATION_DATABASE_URL` must be set to the session pooler (5432). The deploy job refuses to
  run if it is missing.
- Before each migration a `pg_dump` is saved to `/opt/gropto/backups` (14 days kept). It is best effort: if the dump
  fails, for instance because the client is older than the Supabase server, the deploy continues. Keep Supabase's own
  backups on (Project Settings -> Database -> Backups) and consider point-in-time recovery.
- The deploy job also stops early with a clear message if `PROD_ENV` has no `DATABASE_URL` or `JWT_SECRET`, if the URL is
  not `postgresql+asyncpg://`, or if it points at localhost.

## How a deploy works

1. Tests, and every migration applied to an empty Postgres.
2. The image is built and tagged with the commit id.
3. On the server: pull the image, back up the database, run `alembic upgrade head` on Supabase, start the new containers, wait up to two minutes for
   `/health/ready`.
4. If the new version is not healthy, the previous image is started again and the run fails.

Migrations run before the switch, so write them so the previous version still works against the new schema (add columns
before using them, drop them in a later release). A rollback goes back to the old code, not the old schema.

## Everyday tasks

```bash
cd /opt/gropto
docker compose -f docker-compose.prod.yml logs -f api               # logs (set IMAGE=$(cat .current_image) first)
IMAGE=$(cat .current_image) docker compose -f docker-compose.prod.yml run --rm backup   # database backup
```

To roll back by hand, run `bash remote-deploy.sh ghcr.io/<owner>/<repo>:<older commit sha>`.

## Not covered

- The integration tests in `tests/` need a seeded database and a registered device, so CI runs only the database-free
  tests plus the migration check.
- The frontend (Electron app) is not deployed by this pipeline.
