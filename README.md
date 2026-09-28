# Gropto Retail ERP — Backend (Phase 1 + 2 + 3)

FastAPI service implementing the full backend scope from
[BACKEND_IMPLEMENTATION_PLAN.md](BACKEND_IMPLEMENTATION_PLAN.md): Phase 1 (MVP),
Phase 2 (operational ERP expansion) and Phase 3 (enterprise/omnichannel expansion), backed by a
Postgres database (Supabase-hosted or any other Postgres — the app just needs a connection
string). Schema is owned by **Alembic** migrations under `alembic/versions/`; SQLAlchemy models
are the single source of truth the migration is generated from. FastAPI implements all business
logic — auth/RBAC, append-only ledgers, the sync engine, the approval engine, audit logging.

No Docker, no Supabase CLI, no local database is required to work on this code. When you have a
Postgres connection string (Supabase or otherwise), put it in `.env` as `DATABASE_URL` and run
`alembic upgrade head` — see **Setup** below.

## Scope implemented

### Phase 1 — MVP (P0 + P1)
- Auth & RBAC: Argon2id passwords, short-lived JWT + rotating refresh tokens (reuse-detection
  revokes the whole family), device registration/activation, store scoping.
- Products/categories with a revision-watermark `pull` endpoint for device catalogue sync.
- Inventory: append-only movement ledger, cached balances, threshold-gated adjustments.
- POS sync: `POST /api/v1/sync/push` — per-item verdict (`applied|duplicate|conflict|rejected`),
  idempotency-key dedup, role-based discount limits with manager override, loyalty earn/redeem
  (append-only ledger, negative balances flagged not blocked), line-item name/tax snapshots.
- Generic approval engine (`app/services/approvals.py`) — handler registry, re-validates on
  apply, Super Admin applies directly, everyone else queues a request.
- Audit log, written in the same transaction as every sensitive change, no update/delete path.
- Vendors & direct purchase entry (stock-in).
- Excel reports (sales register, inventory snapshot, loyalty, audit log) and staged Excel import
  (products, opening stock) with price changes routed through the approval engine.
- AI dashboard: SQL-computed KPI payload, optional hosted-LLM narrative (degrades gracefully),
  cached with TTL, per-store daily call cap.
- Store & company dashboards.

### Phase 2 — Operational ERP Expansion
- **Returns & refunds** (`app/api/v1/returns.py`): line-level disposition (saleable/damaged/
  vendor return), threshold/age-gated approval, stock effect on approval or immediately.
- **Warehouse basics** (`app/models/models_phase2.py`): warehouses, locations, GRN with
  expected-vs-received variance and QC status, batch/expiry captured on GRN lines.
- **Transfers** (`app/api/v1/transfers.py`): two-sided dispatch/receive, discrepancy detection,
  store-leg stock effects on the Phase 1 ledger.
- **Procurement expansion** (`app/api/v1/procurement.py`): requisitions, PO creation routed
  through the approval engine, GRN receiving that closes the PO and posts stock.
- **Store cash operations** (`app/api/v1/cash.py`): shift open/close, cash-in/out, expected-vs-
  counted variance, store day close aggregating all shifts.
- **Reorder points & replenishment alerts**, **finance foundation** (expenses with approval,
  payables ageing, store P&L snapshot), **fraud/loss-prevention rule scan**
  (`app/api/v1/phase2_misc.py`, `app/services/fraud.py`).

### Phase 3 — Enterprise & Omnichannel Expansion
- **Omnichannel OMS** (`app/api/v1/orders.py`, `app/services/oms.py`): order → stock reservation
  against available-to-promise → pick/pack with substitution → dispatch (OTP generated) →
  delivery confirmation → stock posted to the real ledger only on delivery; cancel releases
  the reservation.
- **CRM** (`app/api/v1/crm.py`): customer 360, RFM segmentation computed from Phase 1 sales
  data, consent flags, campaign audience definition.
- **HR & workforce** (`app/api/v1/hr.py`): employees, shifts, attendance.
- **Enterprise command center** (`app/api/v1/enterprise.py`): store→SKU drill-down, device fleet
  health, and a single "what's wrong right now" exception feed (pending approvals, open fraud
  alerts, unresolved sync failures, transfer discrepancies) — the CEO alerting principle from
  the blueprint, not a wall of reports.

**Deliberately thin, flagged in code comments where it matters:** warehouse-side stock (as
opposed to store-side) isn't yet on its own ledgered balance table; three-way PO/GRN/invoice
matching and vendor rate comparison are structurally wired but not exhaustively validated;
payment gateway processing, GST e-invoice/IRN filing, and tiered loyalty are out of scope
everywhere in this plan, per the original blueprint's Phase 2/3 boundaries.

## Project layout

```
backend/
  alembic/
    env.py                    # loads DATABASE_URL from app settings, target_metadata = Base.metadata
    versions/                 # one revision today: full phase 1+2+3 schema + seed data
  app/
    core/                     # config, db session, JWT/password security
    models/
      models.py                 # Phase 1
      models_phase2.py          # Phase 2 (warehouse, procurement, cash, returns, finance, fraud)
      models_phase3.py          # Phase 3 (OMS, CRM, HR, device config)
      __init__.py                # imports all three so Base.metadata is complete
    schemas/                  # Pydantic request/response models, split the same way
    services/                 # business logic: inventory, loyalty, sync, approvals, audit, ai,
                               # imports, returns, transfers, cash, procurement, finance, fraud,
                               # oms, crm
    api/v1/                   # FastAPI routers, one file per domain, wired in router.py
    main.py
  scripts/seed.py             # bootstrap: one store, one Super Admin, one pending device
```

## Setup

1. **Python environment**
   ```
   python -m venv .venv
   .venv/Scripts/activate       # Windows
   pip install -r requirements.txt
   cp .env.example .env
   ```

2. **Database** — put a Postgres connection string in `.env` as `DATABASE_URL`
   (`postgresql+asyncpg://...`; for a Supabase project use the Session Pooler connection string
   from the project's Database settings). Then run:
   ```
   alembic upgrade head
   ```
   This creates every Phase 1/2/3 table, the shared revision sequence, required indexes, and
   seeds roles/permissions/role-permission grants and the default loyalty config — one
   migration, idempotent extensions (`create extension if not exists`).

3. **Seed bootstrap data** (one store, one Super Admin, one pending device)
   ```
   python -m scripts.seed
   ```

4. **Run**
   ```
   uvicorn app.main:app --reload
   ```
   Docs at `http://localhost:8000/docs`.

5. **First login**
   ```
   POST /api/v1/auth/login
   { "email": "admin@gropto.local", "password": "ChangeMe!123",
     "device_fingerprint": "seed-bootstrap-device" }
   ```
   (Login is email+password only — any device fingerprint works on the first try, no
   approval step. `device_fingerprint` is still recorded for per-counter bill numbering,
   cash sessions, and sync, but it never blocks login.)

## Dev/pilot login credentials

Seeded by `python -m scripts.seed` (Super Admin) and `python -m scripts.seed_test_users` (one
account per other role, all sharing store scope with the Super Admin). **Change every one of
these before any real go-live** — they exist purely so each role can be tested without creating
users by hand.

| Role | Email | Password |
|---|---|---|
| Super Admin | `admin@gropto.local` | `ChangeMe!123` |
| Admin | `admin.test@gropto.local` | `Test@123` |
| Store Manager | `manager@gropto.local` | `Test@123` |
| Cashier | `cashier@gropto.local` | `Test@123` |

Login is **email + password only** — no activation code, no pending/approval gate. A device
that has never logged in before is recorded and active from its very first attempt; there's
nothing to click before it can be used. The `Device` row still exists underneath (bill
numbering, cash sessions, and sync are all scoped to it), it's just never a login blocker.
`GET /auth/devices` lists every device for visibility, and `POST /auth/devices/{id}/revoke`
is the one real control left — revoke a lost or compromised till and it's refused on its next
login attempt. `POST /auth/devices/register` still exists for pre-registering a device (also
active immediately). The Electron frontend generates and remembers its own device fingerprint
automatically, purely for that per-counter identity — it's never required for login to succeed.

## Audit trail (edit log)

Every sensitive write across every module (17 call sites at last count — products, inventory,
approvals, HR, cash, transfers, procurement, returns, auth, loyalty, discounts, devices, ...)
already wrote to the append-only `audit_log` table via `app/services/audit.py`'s `write_audit()`
— immutable at the database grant level (`REVOKE UPDATE, DELETE`), not just app logic.
`app/api/v1/audit.py` adds a filtered, permission-gated (`audit.view`, granted to Super Admin
and Admin) read API over it, store-scoped like every other list endpoint for non-Super-Admin
callers:

- `GET /audit/entries` — filter by `entity_type`, `action`, `user_id`, `store_id`, `date_from`,
  `date_to`; paginated.
- `GET /audit/entity/{entity_type}/{entity_id}` — the full, ordered version history for one
  record (`entity_version` 1, 2, 3, ...), the before-vs-after comparison for every change to
  that one thing over its whole lifetime.
- `GET /audit/actions` — distinct actions that have actually occurred, for a filter dropdown.
- `GET /audit/export` — same filters, streamed as an .xlsx.

Each row records who (`user_id`, `role_code`), where (`store_id`, `device_id`, `ip_address` —
captured once per request in `main.py`'s middleware via a contextvar, not threaded through
every call site), what (`action`, `entity_type`, `entity_id`, `entity_version`), the change
itself (`old_value`, `new_value` as JSON), and why (`reason`, populated wherever the caller gave
one — e.g. every approval-engine request/decision).

## Making future schema changes

Edit the SQLAlchemy models (or add a new `models_phaseN.py` and register it in
`app/models/__init__.py`), then generate a migration against a real database connection:
```
alembic revision --autogenerate -m "describe the change"
alembic upgrade head
```
Autogenerate needs a live DB to diff against — this only works once `DATABASE_URL` points at a
real Postgres instance. Review the generated migration before applying it, same as any Alembic
project.

## Running the test suite

```
pip install -r requirements.txt   # includes pytest + pytest-asyncio
pytest -v
```

`tests/` runs against a real `uvicorn` subprocess the fixtures spin up automatically against
your configured `DATABASE_URL` — not a mock. It needs the dev/pilot credentials above to be
seeded and the `seed-bootstrap-device` device already activated (true once anyone has logged
into the app at least once). Covers: login success/failure/lockout paths, device-registration
enforcement, `/me`, negative authorization (a cashier token hitting `approval.decide`- or
`user.manage`-gated routes gets a real 403, not just a hidden button), and a full real sale
through `POST /api/v1/sync/push` including idempotency-key deduplication on replay. This is a
smoke/integration suite, not exhaustive coverage — the sync engine's concurrent-device and
conflict-resolution paths (§5 of the plan) and the full order lifecycle still need a dedicated
run before production.

## Notes on the hard gates from the delivery plan

- **Sync correctness**: `app/services/sync.py` processes each sale inside its own savepoint, so
  one bad row never blocks the batch; duplicates are caught three ways (idempotency key, sale
  primary key, unique bill number per store+device).
- **Approval re-validation**: handlers in `app/services/approvals.py` re-check the current row
  state before applying and mark the request `stale` if it has moved.
- **Authorization**: `app/api/deps.require_permission` is the single choke point — the device is
  assumed hostile; every mutating endpoint depends on it, not on the client hiding a button.

## Verification status

`alembic upgrade head` has been run against a real Supabase Postgres instance (all Phase
1/2/3 tables + seed data landed with no FK ordering errors), `python -m scripts.seed` and
`python -m scripts.seed_test_users` have populated one user per role, and `uvicorn app.main:app`
boots cleanly against that live database. The Electron frontend has logged in, billed, and
synced against this same backend.

**Not yet exercised end-to-end and worth doing before production**: `POST /api/v1/sync/push`
with a real batch of offline-created bills from two devices (the plan's own hard gate — fifty
bills, zero duplicates); the full order lifecycle `POST /api/v1/orders` → `/pick` → `/dispatch`
→ `/deliver`; the approval engine's re-validate-on-apply path under real concurrent edits; and a
tested backup/restore. `tests/` (see below) covers the read paths and auth/authorization with
automated tests — the sync and order lifecycle gates still need a manual or scripted run against
real data.
