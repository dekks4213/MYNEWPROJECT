# Architecture

Modular monolith, Python 3.12, one repository. Package name `fitcoach` (product brand
"РИТМ / RITM" is configuration: `APP_NAME`, i18n texts).

```
src/fitcoach/
  domain/      pure rules: units/durations, field schemas, food arithmetic + text parser,
               workout blocks + set parser, starter schemas, reminder schedule
  db/          SQLAlchemy models, engine, transaction-scoped RLS identity
  services/    users, diary, food (drafts, catalog, meals, recipes), food_sources,
               media, activities (types/templates/programs/sessions), workout_drafts,
               history, reminders, strong_import, account (export/delete), summary
  ai/          typed task contracts, gateway (consent, budgets, breaker, metering),
               providers: gemini, mock
  bot/         aiogram adapter: middleware, handlers per section, ui (formatting),
               scheduler (reminder loop)
  api/         FastAPI: /healthz, Telegram webhook
  i18n/        ru (default) / en
  prompts/     versioned runtime prompts (*_v1.txt)
migrations/    0001 (P0 schema + RLS), 0002 (P1 tables + RLS + scheduler function)
```

Handlers only present data; services own validation, ownership and persistence; domain code
has no Telegram or DB dependencies.

## Identity and isolation (unchanged from P0, extended to all P1 tables)

- Identity = verified Telegram numeric ID; private chats only.
- `UnitOfWorkMiddleware`: one DB session per update. It binds `app.telegram_id` / `app.user_id` transaction-locally on every transaction begin, records `update_id` for idempotency, commits, and maps `ServiceError` / `AIUnavailableError` to localized messages.
- RLS policies (`owner_id = app_current_user_id()`) on all 17 personal tables. `users` is scoped by Telegram ID. The runtime role is not the owner, has `NOBYPASSRLS`, and has no UPDATE on version tables.
- Same-owner composite FKs, including `food_entries.food_id → foods`, `workout_templates.program_id → programs`, `reminder_deliveries.reminder_id → reminders`.
- Services filter by owner and treat foreign IDs as "not found". IDs inside callback data, FSM data, drafts or model output are never trusted: they are re-resolved with the owner filter before use. A tampered `food_id` in a draft payload is dropped.
- The only cross-user query is `app_due_reminders(now, limit)`, a `SECURITY DEFINER` function. It returns `(reminder_id, owner_id, telegram_id)` so the scheduler can bind each user's own RLS context. It returns no diary data.

## Nutrition

Pipeline:
1. Input — text, photo, voice, copy, saved meal/recipe or a catalog food.
2. `FoodParse` — from the AI or from the deterministic parser.
3. Resolution per item, in order: the user's catalog (`foods`, exact normalized name + brand) → optional external source (USDA generic by English name; Open Food Facts only when the brand matches) → user-stated kcal → labelled AI per-100 g estimate. AI estimates are used for unbranded foods only, never branded ones.
4. Deterministic `compute()` produces the draft.
5. The draft is edited (amount, "350 ккал", remove, add, meal type).
6. Confirm → `food_entries`.

- Amounts: g/kg exact; ml→g only with a labelled density-1 estimate; pieces/servings only with a label serving size or an explicit `grams_estimate` (marked "≈"). Unknown amount → nutrients unknown, never zero.
- Per-basis storage: `foods` keeps nutrients per 100 g, per 100 ml or per serving, plus source, source id, fetch time and preparation. Entries keep amount, unit, grams, nutrients, precision (`measured` / `recipe` / `approximate` / `unknown`) and `nutrient_source`.
- Plausibility: per-100 g energy above 950 kcal or macros above 100 g are rejected.
- Recipes: ingredient totals × (consumed g / cooked yield) or × fraction.
- Meal type: taken from explicit words in the text/caption/transcript or a copied meal; otherwise from local time. The model's guess is ignored.
- Drafts (`drafts` table) carry a `version`. Edits and confirmation are atomic `UPDATE … WHERE status='pending' AND version=:shown`, so stale buttons and concurrent taps cannot double-save.

## AI gateway

`AIGateway` exposes task methods only: `parse_food_text`, `analyze_food_image`, `parse_food_voice`, `parse_workout_text`, `build_activity_draft`. Each goes through one `_run` pipeline:

1. consent — text and media consents are separate;
2. input limits;
3. circuit breaker (3 failures → 60 s open);
4. atomic reservation, per user (calls + media calls) and global per UTC day, committed before the call;
5. provider call under a semaphore (`AI_MAX_CONCURRENCY`) and timeout;
6. Pydantic validation;
7. metering in `ai_calls`: task, model, status, tokens incl. thinking tokens, media type, latency, refunded;
8. reconciliation: the reservation is released when the provider certainly did not process the request (connection error, 429, 4xx config errors).

Content is never stored in logs or metering.

`GeminiProvider`:
- REST `generateContent` with `responseJsonSchema` generated from the Pydantic contracts. The schema is sanitized, because `maxItems` caused HTTP 400 in live tests; limits are re-checked by Pydantic.
- User content is passed as JSON under `untrusted_user_input`; system prompts are versioned files.
- Retries with exponential backoff and jitter on 429/5xx.
- Startup `check()` verifies the configured models; if a model is missing, AI is switched off (fail closed to manual mode).

The model never supplies IDs: activity field keys (`f1`, `f2`…) are assigned server-side, and the contracts forbid extra fields.

## Workout builder

| Concept | Storage |
|---|---|
| Activity type (`kind`: custom/strength/swimming/enduro/moto_ride, `counts_as_training`) | `activity_types` |
| Versioned field definitions | `activity_type_versions.fields` (immutable) |
| Template revisions: field targets + `blocks` (planned) | `workout_template_versions` (immutable) |
| Programs | `programs`, `workout_templates.program_id` |
| Plans | `planned_workouts` (`planned → completed`) |
| Actual sessions: field values, original input, field snapshot, `blocks` (performed), `source`, `source_ref` | `workout_sessions` |

- Blocks (`domain.workout`): `Block(kind, title, rounds, items[Item(name, sets[SetSpec])])`. A `SetSpec` holds reps, load_kg + load_mode (total / per-hand / machine setting), distance, duration, rest, RPE, RIR, warm-up and stroke. The same shape serves gym, swimming and drills.
- Set notation: `60x10` = load × reps; after "60 кг", `3x10` = sets × reps; `4x50м` = repeats × distance. Block headers are «Разминка/Основная/Техника/Интервалы/Заминка».
- Recording from a template shows targets per item. Actual sets are typed; "✓ как в плане" is an explicit user action. Targets never become results implicitly.
- Totals (`totals()`) count only compatible quantities: volume only for kg total loads on working sets, distance in metres, pace from total time ÷ distance. History sums metrics per activity type and per (field key, type, unit) from each session's snapshot. Motorcycle rides are marked "not training", and no calories are derived from them.
- Starters are editable copies with labels in the user's language; custom types need no migration.

## Reminders

- `reminders` (local time, weekday mask, enabled, next_fire_at) and `reminder_deliveries` (unique `(reminder_id, scheduled_for)`, status sending/sent/failed/blocked/skipped).
- One asyncio loop inside the bot process (polling or webhook), every 30 s. For each due reminder:
  1. claim it with `FOR UPDATE SKIP LOCKED`, insert the delivery row and advance `next_fire_at`, commit;
  2. send;
  3. record the outcome.
- Occurrences older than 2 h are skipped, not spammed.
- If the bot is blocked, all of that user's reminders are switched off.
- Quiet hours move a reminder to the end of the window. DST is handled by zoneinfo.
- "Workout" reminders fire only when there is an open plan today; "weigh-in" ones are skipped if the user already weighed in.
- No Redis: PostgreSQL row claiming is sufficient.

## Imports

Strong CSV: comma or semicolon delimiters, known header aliases, unit columns or unit suffixes (lb → kg). Warm-up set order "W" is marked as warm-up. Errors are reported per row, unknown columns are listed. Idempotency comes from the file SHA-256 per user and a per-workout `source_ref` with a unique index. Imported sessions go to the user's strength type.

## Privacy

- Data flow: Telegram → this server (PostgreSQL). With consent, meal/workout text goes to the configured AI provider, and so do photos (re-encoded JPEG ≤ 1280 px, EXIF removed) and voice (OGG).
- Optional food databases receive only a food name/brand query.
- Logs contain exception type + code location only (`logsafe.log_failure`), no message text or tokens.
- Owner-only JSON export; account deletion cascades through every owner FK. Backups: see OPERATIONS.md.

## Known assumptions

- Metric units only.
- FSM state for multi-step input lives in memory, so a restart interrupts an unfinished dialog step. Drafts and records are in PostgreSQL.
- AI budgets count calls; tokens are recorded. Money cost is not computed, because prices change and would need a dated price table.
