# Gropto Retail ERP — Backend Implementation Plan (Supabase)

Adapts the MVP Architecture Plan and the Enterprise ERP Blueprint into a phase-wise backend
build using **Supabase** (Postgres + Auth + Storage + Realtime + Edge Functions) as the
database/backend platform, replacing the originally proposed self-hosted PostgreSQL + FastAPI
VPS stack.

## 0. Architecture Decision: Why Supabase, What Changes

| Original MVP plan | Supabase equivalent |
|---|---|
| Self-hosted PostgreSQL on a VPS | Supabase-managed Postgres 16 (same schema, same constraints) |
| FastAPI service for all business logic | **Postgres functions (PL/pgSQL) + RPC** for transactional logic (sync push, approvals, ledgers); **Supabase Edge Functions (Deno/TS)** for anything needing external calls (AI/LLM, email) |
| Custom JWT + Argon2id auth | **Supabase Auth** (email/phone + password), device-bound refresh sessions via `auth.sessions` + a custom `device_sessions` table |
| Custom role/store authorization checked in API code | **Row Level Security (RLS) policies** on every table, keyed to `auth.uid()` via a `user_stores` / `user_roles` mapping — authorization lives in the database, not app code |
| Object storage for backups/exports | **Supabase Storage** buckets (`exports`, `backups`, `installers`, `imports`) |
| Caddy/TLS/Docker Compose on one VPS | Removed — Supabase hosts DB/Auth/Storage; only a thin Edge Function layer and (optionally) a small FastAPI service remain if custom sync logic outgrows Postgres functions |
| No Redis, no queues | Still true — Supabase's `pg_cron` + `pgmq` (queue extension) cover scheduled jobs and the AI insight cache without adding infrastructure |

**Two structural decisions carry over unchanged and matter even more under RLS:**
- Stock and loyalty points remain **append-only ledgers of deltas** — this is what makes RLS-protected,
  multi-device offline sync safe without server-side locking.
- Every sensitive action still flows through **one generic approval engine** — implemented here as a
  `request_type` enum + `approval_requests` table + a Postgres function per handler, invoked via RPC.

**Local desktop layer is unchanged**: Electron + SQLite remains the offline-first source of truth at
the moment of sale. The outbox syncs to Supabase via a single `sync_push` RPC / Edge Function instead
of a custom FastAPI endpoint.

**Decision to revisit later, not now**: if the sync/approval logic in Postgres functions becomes hard
to test or reason about, promote it to a small FastAPI/Node service sitting in front of Supabase
Postgres (Supabase still provides Auth/Storage/RLS). Do not build that service pre-emptively.

---

## Phase 1 — MVP Foundation (weeks 1–7, matches original delivery plan)

Goal: an offline-first POS that never stops billing, backed by Supabase, with the full P0 feature set.

### 1.1 Supabase Project & Environment Setup (Week 1)
- Create Supabase project (staging + production), pin Postgres version, enable `pgcrypto`, `pg_cron`, `pgmq`, `pg_trgm` extensions.
- Configure Supabase Auth: email/password provider, JWT expiry (short-lived access token), refresh token rotation, custom claims for `role` and `stores` via an `auth.hook` (Auth Hooks / custom access token hook) so every JWT already carries role + store scope.
- Define connection strategy for the desktop app: Supabase client (PostgREST + Realtime) over HTTPS only; no direct Postgres connection from devices.
- Set up `supabase/migrations` as the single source of schema truth; CI runs `supabase db push` to staging on every merge.
- Environments: local Supabase CLI for dev, staging project, production project. Secrets in a `.env` per environment, never committed.

### 1.2 Core Schema & Master Data (Week 1–2)
Tables (all with `id uuid default gen_random_uuid()`, `created_at`, `updated_at`, `revision bigint` from a shared sequence for pull watermarking):
- `stores`, `devices`, `user_profiles` (extends `auth.users`), `roles`, `permissions`, `role_permissions`, `user_stores`
- `categories`, `products` (SKU, barcode, UOM, pack size, MRP, tax, active flag)
- RLS enabled on every table from day one — policies written alongside the table, not retrofitted.
- Seed roles/permissions (Super Admin, Admin, Store Manager, Cashier, Inventory User) as a migration, not UI-managed in P0.

### 1.3 Auth, RBAC & Device Registration (Week 1–2)
- Supabase Auth handles password hashing (bcrypt under the hood) — matches the plan's "identical parameters server and device" intent by having the **device verify against a locally cached hash mirror**, refreshed on every successful online login, for offline login support.
- `device_sessions` table: device fingerprint, one-time activation code issued by Super Admin, last-seen, status (active/revoked). RLS: only Super Admin can register/revoke devices.
- Postgres function `authorize(permission text, store_id uuid)` used inside RLS policies and RPCs — single choke point mirroring the "may this role act, is this store theirs" check from the original plan.
- Custom access token hook injects `role`, `store_ids[]`, `device_id` into the JWT so RLS policies read `auth.jwt()` directly without extra lookups on every row.

### 1.4 Inventory & Movement Ledger (Week 2)
- `inventory_balances` (product × store, cached current value) + `inventory_movements` (append-only ledger: delta, reason, source_type, source_id).
- Unique index on `(source_type, source_id)` in movements → exactly-once application, matching the "movement applies once per source line" constraint.
- Postgres trigger recomputes `inventory_balances` from `inventory_movements` on insert; nightly `pg_cron` job reconciles balance vs ledger sum and flags drift.
- Low-stock alert view + Realtime channel so the desktop dashboard can subscribe live.

### 1.5 POS Domain: Sales, Discounts, Loyalty (Week 3)
- `sales`, `sale_items`, `payments` — sale_items denormalize product name/tax at billing time (deliberate, per original plan).
- `customers`, `loyalty_ledger` (append-only, same pattern as inventory).
- `discount_rules` table + role-based max-percent/max-value columns on `roles`.
- Manager override: `sales.override_user_id` + `override_reason`, written in the same transaction as the sale — no separate approval round-trip for counter discounts.
- Unique idempotency key column on `sales` (`client_idempotency_key`) with a unique constraint — this is the row that makes replay-safe sync possible.

### 1.6 Sync Engine on Supabase (Week 4 — hard gate, per original plan)
Because Supabase has no built-in bidirectional sync, this is custom-built as:
- **Push**: a single Postgres function `sync_push(batch jsonb)` (exposed via RPC) that iterates a batch of outbox rows, applies each inside its own subtransaction using `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING`, and returns one verdict (`applied | duplicate | conflict | rejected`) per item plus the new server revision — mirrors the original per-item-verdict design exactly, just moved from FastAPI into SQL.
- **Pull**: a paged `SELECT ... WHERE revision > :watermark ORDER BY revision LIMIT :page` RPC per master table (products, prices, discount rules), using the shared revision sequence.
- **Health probe**: Supabase's REST health endpoint (or a trivial `SELECT 1` RPC) as the lightweight connectivity check before a push attempt.
- **Conflict/failed-queue table** (`sync_conflicts`, `sync_failures`) written by the same function, surfaced to a Sync Health screen exactly as in the original plan.
- Acceptance gate carries over unchanged: 50 offline bills across 2 devices sync with zero duplicates and correct stock/points.

### 1.7 Approval Engine (Week 5)
- `approval_requests` (request_type, entity_type, entity_id, old_value jsonb, new_value jsonb, reason, requester, store, status, reviewer, review_note, timestamps).
- One Postgres function per `request_type` (price_change, high_stock_adjustment, product_deactivation, high_discount, loyalty_rule_change, user_permission_change, config_change), dispatched through a `handler_registry` lookup table mapping type → function name, invoked via `EXECUTE`.
- Approvals **re-validate on apply**: the handler function re-checks the current row state before applying and marks the request `stale` if it has moved — same guarantee as the original plan, enforced in SQL.
- Every apply writes to `audit_log` in the same transaction (Postgres `SECURITY DEFINER` function ensures this can't be bypassed even if RLS would otherwise block the write).

### 1.8 Audit Log (Week 2, threaded throughout)
- `audit_log` (user, role, store, device, action, entity_type, entity_id, old_value, new_value, timestamp, source, approval_id).
- Implemented as `AFTER INSERT/UPDATE` triggers on every sensitive table, not application-level calls — guarantees no code path can skip it.
- RLS: `UPDATE`/`DELETE` revoked entirely for all roles including Super Admin at the database grant level (not just RLS) — audit rows are physically immutable.

### 1.9 Vendors & Purchases — direct entry only (Week 5)
- `vendors`, `purchases`, `purchase_items` (P1 scope: online-only entry, no PO/GRN workflow yet).

### 1.10 Reporting, Excel Export, Dashboards (Week 5–6)
- Supabase Storage bucket `exports`; export generation as a Postgres function producing a result set, streamed to XLSX by an Edge Function (using a Deno XLSX library), respecting the caller's `store_ids` claim from the JWT.
- Every export call logged to `audit_log`.
- KPI aggregation views (`v_sales_today`, `v_top_movers`, `v_reorder_alerts`, etc.) — same views feed both the dashboard and the AI payload (§1.11).

### 1.11 AI Dashboard (Week 6)
- Edge Function `ai-insights`: queries the KPI views above (never raw tables), builds a compact JSON payload, calls the hosted LLM API, caches the result keyed on a hash of (payload, question) in an `ai_insight_cache` table with a TTL column, enforced via `pg_cron` cleanup.
- AI service uses a **separate Postgres role with read-only grants on the KPI views only** — enforced at the database grant level, matching the original "read-only DB role" guarantee more strongly than an app-level check would.
- Per-store daily call cap via a counter table checked before invoking the LLM.
- Dashboard renders facts from the views independently of whether the LLM call succeeds.

### 1.12 Excel Import — staging gate (Week 6)
- `import_staging_products`, `import_staging_opening_stock` tables (source line number, parsed values, computed action, validation messages).
- Preview → confirm → single transactional apply function, exactly as specified; price changes via import route through the approval engine (one approval request per batch, not per row); opening stock import blocked by a Postgres check if any balance already exists for that store/product.

### 1.13 Security Hardening & Backups (Week 6–7)
- RLS policy test suite (pgTAP or a scripted negative-auth test per role) — every role attempting cross-store or over-privilege access must get zero rows / permission denied.
- Supabase's built-in nightly backups + a scripted `pg_dump` to the `backups` Storage bucket for an independent copy; restore drill onto a scratch Supabase branch before go-live.
- Rate limiting on Edge Functions; Supabase Auth's built-in login attempt lockout.
- MFA enabled for Super Admin accounts (pulled forward from Phase 3, cheap to add via Supabase Auth's built-in MFA).

### 1.14 QA, UAT, Go-Live (Week 7)
- Acceptance criteria unchanged from the original plan (offline billing, sync dedup, negative-auth tests, approval audit trail, export/import correctness) — re-run against the Supabase-backed system.

---

## Phase 2 — Operational ERP Expansion (backend)

Builds on Phase 1 schema by adding new movement types, request types and document tables —
no rebuild, per the original plan's design guarantee.

1. **Returns & Refunds** (highest priority in Phase 2): `returns`, `return_items` linked to original `sales`; approval request type `return_approval` for value/age thresholds; stock disposition as new `inventory_movements` source types (`return_to_saleable`, `return_to_damaged`, `return_to_vendor`).
2. **Warehouse Management**: `warehouses`, `warehouse_locations` (zone/rack/bin), `asn`, `grn`, `grn_items` (variance vs PO), `transfers`, `transfer_items` (two-sided: dispatch + receipt), batch/lot/expiry columns promoted to first-class keys on `inventory_movements`.
3. **Procurement Expansion**: `purchase_requisitions`, multi-level PO approval (new approval request types), `vendor_quotations`, three-way match function (`PO × GRN × invoice` with tolerance), `vendor_performance` materialized view refreshed nightly via `pg_cron`.
4. **Store Cash Operations**: `cashier_shifts`, `cash_movements` (cash-in/out with reasons), `day_close` (denomination count, expected vs counted, variance → approval if beyond tolerance).
5. **Advanced Inventory**: explicit stock-state columns (`reserved`, `in_transit`, `damaged`, `blocked`) on `inventory_balances`; `reorder_points` table; replenishment recommendation view driven by a demand-history function; ageing/dead-stock views.
6. **Finance Foundation**: `expenses`, `payables`, `payment_reconciliation`; store P&L and margin-bridge views; GST-ready export views (fields already captured on `sales`/`sale_items` in Phase 1).
7. **Fraud & Loss Prevention**: rule-based anomaly views (excessive discount by cashier, high void rate, cash mismatch, repeated negative stock) run on a schedule via `pg_cron`, writing to an `alerts` table surfaced to Super Admin — reuses the audit log and approval data captured since Phase 1.

---

## Phase 3 — Enterprise & Omnichannel Expansion (backend)

1. **Omnichannel OMS**: `orders`, `order_items`, stock reservation function against available-to-promise (reads `inventory_balances` minus `reserved`), allocation function (serviceability/distance/workload), `dispatch`, `delivery_confirmation` (OTP + POD), reverse logistics reusing the Phase 2 returns tables.
2. **CRM & Customer Intelligence**: `customer_events` feeding RFM/cohort views; consent columns on `customers`; campaign audience Edge Function; retention/CLV views.
3. **HR & Workforce**: `employees`, `shifts`, `attendance`, productivity views joined against sales/fulfilment throughput; payroll export, not payroll processing.
4. **Enterprise CEO Command Center**: a drill-down view hierarchy (`v_company → v_region → v_store → v_sku → transaction`) backed by materialized views refreshed on a schedule, with exception-driven alert tables rather than raw report dumps.
5. **Enterprise Scalability & Reliability**: Supabase read replicas / connection pooling (Supavisor) for HA; centralized device configuration table pushed via Realtime; fleet health dashboard (`device_sessions.last_seen`, sync error rates); documented RPO/RTO using Supabase's point-in-time recovery; MFA enforcement widened beyond Super Admin.

---

## Cross-Cutting: What Moves to Postgres/RLS vs. Stays in Application Code

- **Authorization**: fully in RLS + the `authorize()` function — the desktop/Edge Function layer never needs a parallel permission check, closing the "server assumes the client is hostile" gap at the data layer itself.
- **Idempotency & conflict resolution**: fully in Postgres functions/constraints (unique idempotency key, unique bill number per store+device, unique movement source reference) — no in-memory dedup anywhere.
- **Orchestration that calls external services** (LLM calls, Excel file generation, transactional email): Edge Functions, since Postgres functions can't make HTTP calls.
- **Everything else** (approval handlers, audit triggers, ledger math, sync push/pull): Postgres functions, kept close to the constraints they protect.

## Suggested Backend Delivery Order (condensed)

| Phase | Weeks | Backend focus |
|---|---|---|
| 1 | 1–2 | Supabase project, schema, RLS, auth, device registration, inventory ledger |
| 1 | 3 | POS domain: sales, discounts, loyalty ledger |
| 1 | 4 | Sync engine (hard gate) |
| 1 | 5 | Approval engine, vendors/purchases (direct entry) |
| 1 | 6 | Reporting/export, AI dashboard, Excel import staging |
| 1 | 7 | Security hardening, backup/restore drill, QA |
| 2 | — | Returns, warehouse/WMS, procurement expansion, cash ops, advanced inventory, finance foundation, fraud rules |
| 3 | — | Omnichannel OMS, CRM, HR, enterprise command center, HA/scale |

Nothing in Phase 2 or Phase 3 requires re-keying Phase 1 tables — new document types and new
`inventory_movements`/`loyalty_ledger` source types are additive, and new approval request types
plug into the existing handler registry.
