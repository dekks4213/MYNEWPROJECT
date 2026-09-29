# РИТМ (RITM) — repository instructions

Self-hosted multi-user Telegram nutrition/training diary (package `fitcoach`). Read `docs/STATUS.md` first (phase, what is verified, next step), then `docs/ARCHITECTURE.md`. Do not redesign from scratch. Production target: Windows 11 + Docker Desktop (WSL 2); nothing may depend on Unix-only host behaviour.

- Communicate with the owner in Russian. Code and developer docs in English. Bot UI: Russian default + English (`src/fitcoach/i18n/`). Every user-facing key must exist in both catalogs (`tests/test_i18n.py`).
- Layers: `domain` (pure) → `services` (validation, ownership, persistence) → `bot`/`api` (presentation only).
- Every personal table needs `owner_id`, an RLS policy, same-owner composite FKs and runtime grants, all added in the migration. Never use the owner role at runtime. Never trust IDs from callbacks, FSM data, drafts or model output.
- Unknown values are `None`, never 0. Plans and targets are never performed work. Version rows are immutable.
- Confirm "saved" only after `session.commit()`. Raise `ServiceError(code)` for user-facing errors (i18n key `err.<code>`).
- AI only produces drafts; users confirm. Manual paths must work with `AI_PROVIDER=disabled`. Model names come only from config. Add AI tasks as typed methods on `AIGateway`, not generic calls.
- Logging: use `logsafe.log_failure`. Never log message text, tokens or health data. Never commit `.env`.
- Medical modules (supplements, meds, labs) are out of scope until P1 is stable.

Commands:
- `uv sync`
- `make test-db && make test`, `make lint` (ruff + mypy strict)
- `make dev`, `make migrate`, `make backup`, `make restore-check FILE=…`, `make smoke` (live Gemini, costs money)

DB tests need `TEST_PG_ADMIN_URL`. The live AI test needs `RITM_LIVE_GEMINI=1`.

Update `docs/STATUS.md` with exact checks and outcomes after each work session.
