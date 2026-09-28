# Status

Last updated: 2026-09-28. Current phase: **P0 implemented, live Telegram/AI verification pending.**

## Implemented (P0)

- Config (pydantic-settings), `.env.example`, Dockerfile (non-root), Docker Compose (db + one-shot migrate + bot; DB port not published; app port on 127.0.0.1 only).
- Alembic migration `0001`: schema, RLS on all personal tables, same-owner composite FKs, CHECK constraints, immutability triggers on version tables, least-privilege grants for the runtime role. Role bootstrap: `deploy/postgres/10-roles.sh`.
- Transaction-scoped RLS identity; update-level idempotency; commit-before-confirm; localized error handling.
- Onboarding (resumable, DB-backed), settings (language, time zone, AI consent, kcal target).
- Manual food (step-by-step and one-line), weight, daily summary with unknown-value accounting, corrections (kcal edit with version check, soft delete/restore).
- Activity builder via buttons: custom type with duration + any fields, templates with targets, plan today/tomorrow, record from plan/template/type, archive template. Service-level type/template revisions with history preservation.
- AI gateway: disabled/mock/gemini, consent, input limit, atomic per-user/global daily call budgets, circuit breaker, timeout, metering; meal draft → confirm/cancel (idempotent).
- ru/en localization with completeness tests.

## Checks performed (2026-09-28, this environment)

| Command | Result |
|---|---|
| `TEST_PG_ADMIN_URL=postgresql://postgres@127.0.0.1:5433/postgres uv run pytest -q` (local PostgreSQL 16.13) | 63 passed |
| `make test-db && make test` (postgres:16 container) | 63 passed |
| `make lint` (ruff check, ruff format --check, mypy --strict) | clean |
| `docker compose up db migrate` (image built with a sandbox-only CA override) | roles created, migration `0001` applied, 12 RLS policies, tables owned by `fitcoach_owner` |
| `docker compose up bot` with `BOT_MODE=webhook`, fake token, no webhook registration | `/healthz` → `{"status":"ok"}`; webhook without secret → 401; process runs as uid 10001 |

Test coverage includes: cross-user reads/writes, forged nested IDs and callbacks, composite-FK rejection, pooled-connection context isolation (pool of 1, commit and rollback), immutable versions, migration up/down/up, plan ≠ completion, targets not copied, template/type revision history, archive, optimistic edit conflict, idempotent delete/restore/draft confirm/plan completion/duplicate update, unknown ≠ zero, decimal commas, duration formats, timezone day boundaries, AI failure/budget/consent/circuit breaker with manual continuity, parallel budget reservations, webhook secret/size/malformed body, full two-user acceptance flow with simulated restart, English UI.

## Mocked / not verified

- **Telegram**: all bot tests use a recording fake Bot API session. No live bot token was used. Live check pending: run `make dev` with a real `BOT_TOKEN`, onboard two real accounts, repeat the acceptance flow.
- **Gemini**: adapter written against the documented v1beta `generateContent` REST shape but **not verified** — no key, and Google docs/API hosts were not reachable from this environment. Before enabling: confirm field names (`responseSchema`), model name, regional availability, data terms and pricing; add a guarded live smoke test.
- **Docker build as committed**: verified only with a sandbox-specific CA/proxy override (the sandbox blocks ghcr.io and intercepts TLS). The committed Dockerfile installs uv from PyPI; build on the target server not yet run.
- Nothing is deployed.

## Known limitations

- FSM state for multi-step input is in memory (single process; interrupted inputs restart after a process restart).
- Metric units only. No field "required" toggle in the Telegram builder (all custom fields optional; service supports `required`).
- Type revision (adding fields later) exists in services/tests but has no Telegram UI yet; template revision likewise.
- No export/deletion, backups, reminders, food databases, photo/voice, Strong import, cost-in-money tracking (P1).
- Numbers in bot messages use a dot as decimal separator in both languages.

## Next executable step

1. Live P0 check on the server: `cp .env.example .env`, fill passwords + `BOT_TOKEN`, `make dev`, run the acceptance flow from two Telegram accounts, restart with `docker compose restart bot`, verify summaries.
2. Start P1 with account export/deletion and encrypted backups (`make backup`, `make restore-check`), then the full workout builder (blocks/intervals) and Telegram UI for type/template revisions.
