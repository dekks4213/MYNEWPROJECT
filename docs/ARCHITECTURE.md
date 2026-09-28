# Architecture

Modular monolith, one repository, Python 3.12.

```
src/fitcoach/
  domain/      pure rules: number/duration parsing, field schemas, nutrition totals
  db/          SQLAlchemy models, engine, transaction-scoped RLS identity
  services/    application services (users, diary, activities, drafts, summary)
  ai/          typed AI gateway, providers (mock, gemini), budgets, circuit breaker
  bot/         aiogram adapter: middleware, handlers, keyboards, formatting
  api/         FastAPI: /healthz and Telegram webhook
  i18n/        ru (default) / en catalogs
  prompts/     versioned runtime prompts (meal_extraction_v1)
migrations/    Alembic (owner role only)
```

Domain code has no Telegram or DB dependencies. Handlers do presentation only; services own validation, authorization and persistence.

## Identity and isolation

- A user is identified by the verified Telegram numeric ID from the update (never username or client-supplied IDs). Only private chats are processed.
- `UnitOfWorkMiddleware` opens one DB session per update, resolves the user and binds `app.telegram_id` / `app.user_id` with `set_config(..., is_local => true)` on **every** transaction begin (`after_begin` event). The context ends at COMMIT/ROLLBACK and cannot leak to the next pooled connection (tested with a pool of 1).
- Every personal table has `owner_id` and an RLS policy `owner_id = app_current_user_id()`; `users` is scoped by Telegram ID.
- Runtime role `fitcoach_app`: not owner, `NOBYPASSRLS`, no UPDATE on version tables. Migrations use `fitcoach_owner`.
- Composite foreign keys `(ref_id, owner_id) → parent(id, owner_id)` prevent cross-user references even if service checks were bypassed.
- Services also filter by `owner_id` and return "not found" for foreign IDs (callback data is untrusted).

## Integrity

- Telegram `update_id` is recorded in `processed_updates` inside the handler transaction; duplicates are skipped. Markers older than 7 days are purged at polling start.
- "Saved" is sent only after `commit()` succeeds. Expected errors roll back and show a localized message.
- Edits use version checks (`version` column; `UPDATE … WHERE version = :expected`).
- Draft confirmation and plan completion are atomic `UPDATE … WHERE status = 'pending'/'planned'`.
- Deletes are soft (`deleted_at`) and reversible.

## Universal activity builder (P0 subset)

| Concept | Table | Notes |
|---|---|---|
| Activity type | `activity_types` | user-owned, archivable |
| Field definitions | `activity_type_versions.fields` (JSONB) | immutable versions, validated by `FieldSchema` |
| Template | `workout_templates` + `workout_template_versions` | immutable revisions with planned targets |
| Plan | `planned_workouts` | status `planned → completed`; never implies performed work |
| Actual session | `workout_sessions` | values + original input + field snapshot + names at the time |

Fields are declarative data: `key` (stable, `f1`, `f2`…, independent of label), `label`, `type` (decimal, integer, duration, selection, boolean, text), `unit`, `required`, `aggregation`, `duration_format`, `choices`, bounds. No formulas or code are ever executed. A key's type cannot change between versions.

Durations are stored in seconds. For `h:mm` fields `1:30` = 90 min and a bare number = minutes; for `mm:ss` fields `1:30` = 90 s and a bare number is rejected as ambiguous. Decimals accept commas and are stored as exact strings. Skipped values are absent (unknown), never zero, and targets are never copied into performed values.

Blocks, intervals, repeat groups, multiple programs and cross-session aggregation are P1.

## AI gateway

`AIGateway.parse_meal` checks: provider enabled → user consent → input length → circuit breaker → atomic budget reservation (per user and global, per UTC day, row-locked `INSERT … ON CONFLICT DO UPDATE … WHERE calls < limit`) → provider call with timeout and concurrency limit → Pydantic validation → metering row in `ai_calls` (status, tokens; never content). Reserved calls are not refunded on failure (conservative). Any failure raises `AIUnavailableError`; manual logging is unaffected.

The meal prompt extracts only what the user stated; nutrient numbers are null unless stated. User text is passed as JSON data, not concatenated into instructions.

## Assumptions (conservative, P0)

- Metric units only; imperial is planned.
- Adult-only product: users who do not confirm 18+ cannot proceed.
- No automatic calorie/energy recommendations: targets are user-entered.
- FSM (in-progress multi-step input) is in memory; a restart interrupts an unfinished input but never loses saved records or onboarding progress. Webhook mode assumes one process.
- AI budget counts calls, not money; token counts are recorded when the provider reports them. Prices are not yet tracked.
- "Day" for diary entries is the user's local date from their confirmed IANA time zone; AI budgets use the UTC date.

## Privacy

- Data flow: Telegram → this server (PostgreSQL). With AI enabled *and* user consent, the text of a meal description goes to the configured provider. Nothing else leaves the server.
- Logs contain no message text, tokens or health data; aiogram event and httpx request logs are raised to WARNING.
- Export, account deletion, backups and retention are P1 and not implemented yet.
