.PHONY: dev down logs migrate test test-db test-db-stop lint fmt backup restore-check smoke

TEST_DB_PORT ?= 55432
export TEST_PG_ADMIN_URL ?= postgresql://postgres:test@127.0.0.1:$(TEST_DB_PORT)/postgres

dev:            ## build and start db + migrations + bot
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f bot

migrate:        ## apply migrations with the owner role
	docker compose run --rm migrate

test-db:        ## disposable PostgreSQL for tests on 127.0.0.1 only
	docker run -d --rm --name fitcoach-test-db -e POSTGRES_PASSWORD=test \
		-p 127.0.0.1:$(TEST_DB_PORT):5432 postgres:16
	until docker exec fitcoach-test-db pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; do sleep 1; done

test-db-stop:
	docker stop fitcoach-test-db

test:           ## unit + integration + bot e2e tests (needs TEST_PG_ADMIN_URL)
	uv run pytest -q

lint:
	uv run ruff check src tests migrations
	uv run ruff format --check src tests migrations
	uv run mypy

fmt:
	uv run ruff format src tests migrations
	uv run ruff check --fix src tests migrations

backup:         ## pg_dump into ./backups (Windows: scripts/backup.ps1)
	scripts/backup.sh

restore-check:  ## restore a dump into a temporary DB and compare counts: make restore-check FILE=...
	scripts/restore-check.sh $(FILE)

smoke:          ## live Gemini smoke test (max 7 calls; needs GEMINI_API_KEY/GEMINI_MODEL)
	uv run python scripts/smoke_gemini.py
