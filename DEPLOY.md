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
JWT_SECRET=<long random string>            # python -c "import secrets; print(secrets.token_urlsafe(48))"
ENVIRONMENT=production
CORS_ORIGINS=app://.,https://your-domain
REDIS_URL=rediss://...                     # Upstash
SITE_ADDRESS=api.your-domain.com           # leave empty to serve plain HTTP on the IP for now
```

For the AI assistant add (the key is a secret: only in `PROD_ENV`, never in git):

```
AI_PROVIDER_KIND=openai
AI_PROVIDER_BASE_URL=https://api.openai.com/v1/chat/completions
AI_MODEL=gpt-4.1-mini                      # cheap and quick; gpt-4.1-nano is cheaper still
AI_PROVIDER_API_KEY=sk-...
AI_DAILY_CALL_CAP=200                      # stops a runaway bill
```

Point the domain's DNS `A` record at the server before setting `SITE_ADDRESS`, so Caddy can get the certificate. Add the
payment, WhatsApp, SMS and email keys when you use them.

**5. First deploy.** Push to `main`, or Actions -> CI/CD -> Run workflow.

Then check it: `curl https://<your-domain>/health/ready`

## Supabase and Upstash (already set up)

The database (Supabase) and Redis (Upstash) are already created and migrated, so a normal deploy **only ships the backend**:
it does not run migrations and does not touch the database or Redis. CI never touches them either (it proves the migrations
on a throwaway Postgres).

- `PROD_ENV` carries the **production** connection strings (the password there is the production one, not your local
  `.env`'s): `DATABASE_URL` (`postgresql+asyncpg://...`, Supabase pooler) and `REDIS_URL` (`rediss://...`, Upstash).
- Session pooler (port 5432) caps at 15 connections, so keep `WEB_CONCURRENCY=1`.
- The deploy job stops early with a clear message if `PROD_ENV` has no `DATABASE_URL` or `JWT_SECRET`, if the URL is not
  `postgresql+asyncpg://`, or if it points at localhost.
- **When you add a migration later:** merge it, then run Actions -> CI/CD -> Run workflow and tick "Also apply database
  migrations". That takes a best-effort `pg_dump` first, then runs `alembic upgrade head`, then deploys. For this,
  `PROD_ENV` also needs `MIGRATION_DATABASE_URL` (session pooler, port 5432).

## How a deploy works

1. Tests, and every migration applied to an empty Postgres.
2. The image is built and tagged with the commit id.
3. On the server: pull the image, start the new containers, wait up to two minutes for `/health/ready`. (Migrations only
   when you ask for them, see above.)
4. If the new version is not healthy, the previous image is started again and the run fails.

When you do run migrations, they run before the switch, so write them so the previous version still works against the new
schema (add columns before using them, drop them in a later release). A rollback goes back to the old code, not the old
schema.

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

## Paytm POS machine (card and UPI at the till)

The till sends the bill's exact amount to the store's Paytm machine, the customer pays on the machine, and the server
asks Paytm whether it was paid. The bill is settled only when Paytm confirms it; "sent" is never treated as paid.

### What you need from Paytm first
- Wireless POS (ECR) integration switched on for your merchant and machine. Until Paytm does this, the Sale API answers with a
  merchant configuration error and nothing reaches the machine.
- The **merchant key** and the **channel id** for the integration (never put these in the app, git or chat).
- For production: the **production API host** from Paytm's activation mail. It is not guessed here: set it as `PAYTM_BASE_URL`.
  Staging uses `https://securegw-stage.paytmpayments.in`.

### Setting it up
1. Add to `PROD_ENV`: `PAYTM_ENVIRONMENT=staging`, `PAYTM_MERCHANT_KEY=...`, `PAYTM_CHANNEL_ID=...` (and `PAYTM_BASE_URL` for production).
2. Run the workflow with "Run database migrations" ticked once (adds the `paytm_terminals` and `paytm_pos_payments` tables).
3. Map the machine to its store. On the server, with `PAYTM_MID`, `PAYTM_TID` and `PAYTM_DEVICE_SERIAL` in the environment:
   `docker compose -f docker-compose.prod.yml run --rm api python -m scripts.seed_paytm_terminal --store-code ST001 --label "Counter 1"`
   (or an admin can call `PUT /api/v1/payments/paytm-pos/terminals`). One row per machine; each store has its own.
4. `GET /api/v1/payments/config?store_id=<store>` should now say `"enabled": true`.

### Testing on the real machine (staging first)
1. Open Billing, add an item, press **Card (F5)** or **UPI (F6)**. A "Paytm POS" window shows the amount and the machine's TID.
2. Press **Send to Paytm**. The machine should show the same amount (the cashier may have to accept it on the machine; nothing accepts it for them).
3. Pay on the machine. Within about 10 to 20 seconds the window says "Payment received" and the bill settles.
4. Also try: decline on the machine (window says failed, Try again works), **Cancel payment** while waiting, and unplug the machine (window keeps waiting, then offers cancel).
5. Open Activity Log: every state change of the payment is listed (`paytm.payment_status`).
6. Only after a ₹1 payment works end to end on staging, move to production keys and host.

### Rules the server enforces
- One request at a time per machine; pressing the button twice returns the same request (idempotency key).
- A bill that carries a Paytm reference is rejected unless that payment is confirmed paid, for the same store and the same amount, and not already used by another bill.
- Paytm is asked for the status no more often than every 10 seconds. A result that cannot be trusted is `unknown` and blocks a new request on that machine until it is cancelled or resolved.

### Not covered / to verify on the machine
- Paytm's pages do not say whether the Void API also cancels a request the customer has not paid yet. Cancel is implemented through it and is only marked cancelled when Paytm confirms it; if it does not work for unpaid requests, cancel on the machine itself.
- There is no webhook: Paytm's Wireless POS pages describe status enquiry only.
- Needs internet at the till. Offline, card and UPI are recorded by hand as before.
- If the app closes after the customer paid but before the bill is saved, the payment is on the server (`paytm_pos_payments`) and on the machine, but the bill is not recreated automatically.
