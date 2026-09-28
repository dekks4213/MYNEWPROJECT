# Fit Coach — repository instructions

Self-hosted multi-user Telegram nutrition/training diary. Read `docs/STATUS.md` first (current phase, what is verified, next step) and `docs/ARCHITECTURE.md` before changing design. Do not redesign from scratch.

- Communicate with the owner in Russian. Code, identifiers and developer docs in English. Bot UI: Russian default + English (`src/fitcoach/i18n/`); every new user-facing key goes into both catalogs (`tests/test_i18n.py` enforces it).
- Layers: `domain` (pure) → `services` (validation, ownership, persistence) → `bot`/`api` (presentation). No business rules in handlers.
- Every personal table: `owner_id`, RLS policy, same-owner composite FKs, grants for the runtime role — add them in the migration. Never use the owner role at runtime. Never trust callback/client IDs.
- Unknown values are `None`, never 0. Plans/targets are never performed work. Version rows are immutable.
- Confirm "saved" only after `session.commit()`. Raise `ServiceError(code)` for user-facing errors (`err.<code>` key).
- AI only drafts; users confirm. Manual paths must work with `AI_PROVIDER=disabled`. Model names only via config.
- Never commit `.env`, print secrets, or log message text/health data.

Commands: `uv sync`, `make test-db && make test`, `make lint`, `make dev`, `make migrate`. DB tests need `TEST_PG_ADMIN_URL`.
Update `docs/STATUS.md` with exact checks and outcomes after each work session.
