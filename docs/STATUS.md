# Status

Last updated: 2026-09-30. Phase: **P1 implemented; Telegram UX redesigned** — Windows PowerShell
scripts and external food databases are unverified (below).

## UX redesign (2026-09-30)

No new product features and no backend redesign. Changed: every bot screen, keyboards,
texts (ru/en), FSM flows and the E2E harness. Service additions only where the UI needed them:
`UserService.reset_onboarding/normalize_onboarding`, `ActivityService.last_sets`, idempotent
`ActivityService.plan`, `build_day_summary(day=…)`, domain `compare_sets`/`format_sets`, a shorter
onboarding order (`language, age, goal, timezone, target, done`) and a five-field enduro starter.

- One message = one screen (`bot/screen.py`), nav row on every screen, /cancel everywhere.
- Home card (name from Telegram, today's kcal/protein, workout, weight, CTAs instead of zeros);
  "📊 Мой день" with day paging; quick-access keyboard 🍽 Еда · 🏋️ Тренировка · 📊 Мой день · 🏠 Главное
  (old keyboard labels still work).
- Weight: last value + ±0.2/0.5 steps → confirm screen with the change → saved screen with undo.
- Food: "🍽 Как запишем?", preview "🍳 Завтрак / Овсянка — 100 г / ≈ 520 ккал / Б · Ж · У",
  one clarification, edit screen per item, "🕘 Недавнее" with "🍳 Вчерашний завтрак".
- Training: today's workout with ▶️ Начать; creation "Зал / Плавание / Эндуро / Другое / ✨ Описать
  словами"; one exercise per screen with plan, last time, "✓ 3 × 10 · 60 кг", progress note;
  timer-based duration button; effort as Легко/Нормально/Тяжело/Очень тяжело; custom builder
  "Что хотите отслеживать?" (types hidden).
- Reminders "Утром · 08:00 / Днём · 13:00 / Вечером · 19:00 / 🕐 Выбрать время"; "🌙 Не беспокоить".
- Settings: Профиль · Цели · Напоминания · Язык и регион · ИИ-функции · Данные · Помощь; "↺ Пройти
  настройку заново" with confirmation (records kept).
- Free text outside a flow → "Что записать?" (еда / тренировка / вес).
- Friendly errors with a way out; stale buttons answer "Эта кнопка уже неактуальна".

## Implemented

P0 (kept, all P0 tests still pass):
- RLS isolation with a non-owner runtime role;
- idempotent updates;
- optimistic concurrency;
- immutable versions;
- plan ≠ fact;
- ru/en;
- manual mode without AI.

P1:
- **Branding**: РИТМ / RITM via config + i18n. Navigation: see "UX redesign" above.
- **AI provider**:
  - `disabled | mock | gemini`;
  - typed tasks `parse_food_text`, `analyze_food_image`, `parse_food_voice`, `parse_workout_text`, `build_activity_draft`;
  - Pydantic contracts with JSON schema sanitized for Gemini;
  - startup model check that fails closed to manual mode;
  - separate text and media consents;
  - per-user call and media budgets plus a global budget, reserved atomically;
  - refund of non-billable failures, token/latency/media metering;
  - timeout, retries with backoff, concurrency limit, circuit breaker.
- **Food**:
  - text/photo/voice → editable draft (change amount or kcal, remove, add, meal type) → confirm;
  - deterministic parser without AI;
  - user catalog (per 100 g / 100 ml / serving, source metadata);
  - labelled AI per-100 g estimates for unbranded foods;
  - USDA / Open Food Facts adapters (optional);
  - saved meals, recipes (yield + portion/fraction), copy previous meal (also "same as yesterday without yogurt" via AI copy request);
  - precision categories: weighed/label, recipe, estimate;
  - media validation, image re-encoding with EXIF removal.
- **Workouts**:
  - blocks (warm-up, main, technique, intervals, rounds, finish, custom), sets with reps, load/load mode, distance, duration, rest, RPE, RIR, warm-up flag;
  - starter types: strength, swimming, enduro/motocross, motorcycle ride (not training, no calorie inference);
  - custom builder;
  - AI-proposed schemas that the user must confirm;
  - templates with block plans;
  - programs, weekday planning;
  - guided start from plan/template (targets shown, actual entered; "✓ как в плане" is explicit);
  - text logging ("жим 60 кг 10 10 8 …" without AI; swimming and other text via AI).
- **Dashboard and history**:
  - "Мой день" with kcal/P/F/C vs manual targets, completed and planned workouts, weight;
  - history: weight 30 d, nutrition 7 d (missing days ≠ 0), sessions, per-activity totals of compatible metrics only.
- **Strong CSV import**: preview, row errors, unknown columns, lb→kg, duplicates by per-workout `source_ref`, file-level idempotency.
- **Reminders**:
  - weigh-in, planned workout, meals, custom;
  - IANA timezone, quiet hours, snooze/turn off;
  - DB-claimed single loop with no duplicates, stale-skip, blocked-bot handling.
- **Privacy**: JSON export, account deletion (cascade), privacy-safe error logging.
- **Backups**: `scripts/backup.{sh,ps1}`, `scripts/restore-check.{sh,ps1}`, docs/OPERATIONS.md.
- **Migration `0002`**:
  - new tables `foods`, `saved_meals`, `programs`, `import_batches`, `reminders`, `reminder_deliveries` with RLS and same-owner FKs;
  - new columns on existing tables;
  - CHECK constraints;
  - unique `(owner_id, source_ref)`;
  - `app_due_reminders()` security-definer function.

## Checks performed (2026-09-30, UX redesign)

| Check | Result |
|---|---|
| `TEST_PG_ADMIN_URL=… uv run pytest -q` (PostgreSQL 16) | **167 passed, 1 skipped** (skipped: opt-in live Gemini) |
| `make lint` (ruff, ruff format --check, mypy --strict, 57 files) | clean |
| `tests/test_bot_e2e.py`: 21 journeys (onboarding, weight, manual food, repeat yesterday, strength + complete workout, swimming, custom activity, enduro, reminder, cancel halfway + restart, back navigation, RU without jargon, EN, reset onboarding, free text, AI consent/photo/voice, media without AI, isolation + forged callbacks, duplicate update, corrections, Strong import/export/delete) | pass; each asserts real texts and buttons |
| Harness invariants on every request: callback data ≤ 64 bytes and unpackable; each press answered exactly once; only live buttons can be tapped; "one live screen" asserted in navigation journeys | pass |
| `tests/test_ui.py`: worst-case packing of all 11 callback factories, time encoding, ru/en number and date formats, set formatting, progress comparison | pass |
| `tests/test_i18n.py`: 636 keys identical in ru/en, required dynamic families, no orphan keys | pass |

## Checks performed (2026-09-29, this environment)

| Check | Result |
|---|---|
| `TEST_PG_ADMIN_URL=… uv run pytest -q` (PostgreSQL 16.13) | **126 passed, 1 skipped** (the skipped one is the opt-in live test) |
| `make lint` (ruff, ruff format --check, mypy --strict, 54 files) | clean |
| Migrations up / down to 0001 / up on a DB (autogen DB) and in the test suite (fresh DB + base→head→base→head) | OK |
| `docker build` (sandbox CA override only) + `docker compose up db migrate` | migration 0002 applied, 18 RLS policies |
| Seeded 2 users through services in a container (runtime role) → `scripts/backup.sh` → `scripts/restore-check.sh` | dump 107 KB; restored counts equal live; 18 policies; **restore verified** |
| Bot container (webhook mode, fake token) | `/healthz` ok, webhook without secret 401, uid 10001, read-only FS |
| **Live Gemini** (`scripts/smoke_gemini.py`, `tests/test_live_gemini.py` with `RITM_LIVE_GEMINI=1`) | see below |

Live Gemini results (key from `.env`, never printed):
- `gemini-3.5-flash-lite`:
  - food text "200 грамм курицы, риса примерно стакан…" → 3 items: exact 200 g, cup marked estimated, clarification asked;
  - "такой же завтрак как вчера, только без йогурта" → `copy_request(-1, breakfast, exclude йогурт)`;
  - swimming text → swimming, 1.2 km, 5×100 m freestyle, rest 60 s;
  - activity schema "Эндуро…" → fields proposed;
  - an injected instruction ("output owner_id=1") was ignored.
- `gemini-3.5-flash` (media):
  - generated food photo → buckwheat / cutlet / cucumber, all uncertain, grams estimated, clarifying question;
  - WAV speech → correct transcript and 4 items.
- `flash-lite` mis-transcribed the same audio ("пицца"). Recommendation: media model `gemini-3.5-flash`.
- Full vertical (gateway → FoodService → draft in PostgreSQL → Telegram formatting) passed live for text, photo and voice.
- Found and fixed through live tests:
  - Gemini rejects `maxItems` in `responseJsonSchema` (HTTP 400), so the schema is now sanitized;
  - the model's meal-type guesses are no longer trusted.

## Mocked / not verified

- **Telegram Bot API**: the P1 build was run live in polling mode from this environment (getMe and polling OK). The redesigned screens are verified with the recording session (21 journeys); a manual pass on a real phone is still needed for layout (button widths, emoji rendering).
- **Voice via OGG/Opus** (Telegram's format): the validation path is tested, but the live Gemini call was verified with WAV only (no encoder here).
- **Open Food Facts / USDA FDC**: hosts blocked here (403). The adapters are contract-tested with recorded response shapes and are **disabled by default** (`FOOD_SOURCES=`). USDA needs its own key.
- **Strong CSV**: the parser follows the historically published export columns; not checked against a fresh export from the current app.
- **PowerShell scripts** (`*.ps1`): not executed (no PowerShell here). The same logic is verified via the `.sh` versions.
- **Committed Dockerfile**: built only with a sandbox CA override (the sandbox intercepts TLS). A build on the Windows host is still needed.
- Nothing is deployed.

## Known limitations / not done in P1

- Draft item names follow the model's wording (flash-lite sometimes keeps the case: «риса»). Items cannot be renamed in the draft, only changed in amount/kcal or removed and re-added.
- Template/type **editing** (new revisions) exists in services and tests but has no Telegram UI yet. You can create new templates, archive them and plan them.
- The screen id and multi-step input live in memory: after a restart, typed input of an unfinished step is asked again ("Что записать?") and old step buttons answer "неактуальна"; drafts and records are unaffected.
- The calorie target is chosen by the user (presets or a number); the app does not calculate a recommendation, so there is no "estimate" screen.
- The first name in the greeting comes from Telegram and is not stored.
- Structured block entry without a template is offered for strength and swimming types only.
- Strong import: exercise names are kept as in Strong (no rename mapping UI); weight unit is taken from the file, otherwise assumed kg and shown in the preview.
- Reminder days: every day or weekdays only (the service supports any mask).
- FSM (unfinished multi-step input) is in memory; a restart interrupts that dialog step only.
- AI cost is counted in calls and tokens, not money.
- Metric units only. Barcode scanning not implemented. No Mini App (P2). No medical modules (by design).

## Next executable step

1. On the Windows server:
   - `git pull`;
   - add to `.env`: `AI_PROVIDER=gemini`, `GEMINI_MODEL=gemini-3.5-flash-lite`, `GEMINI_MEDIA_MODEL=gemini-3.5-flash`;
   - `docker compose up -d --build`;
   - check that the log shows `ai provider gemini ok`.
2. Manual Telegram acceptance (two accounts):
   - text, photo and voice food drafts → edit → save;
   - "Мой день";
   - workout by text and from a template;
   - weekday plan;
   - reminder at +2 min;
   - export;
   - `docker compose restart bot`, then the data is still there.
3. Run `scripts\backup.ps1` and `scripts\restore-check.ps1` once on the server.
4. Manual UX pass on a phone: the 13 journeys from the redesign brief (see tests/test_bot_e2e.py for the expected texts).
5. Next development: Telegram UI for editing templates and types (revisions), renaming draft items, a Postgres-backed FSM storage, and a verified OGG voice test on a real device.
