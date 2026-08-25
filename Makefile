# Vigil dev targets (require: uv, docker)
#
# The repo-root .env holds production values (live Neon, real Slack, real
# Gemini key). Every target that builds Settings() therefore goes through
# scripts/safe-run.sh, which pins local-only overrides. Run a target bare and
# it writes to production. Tests are exempt: conftest.py pins its own env.
.PHONY: dev db test test-all lint seed demo check-env

SAFE := ./scripts/safe-run.sh

db:            ## start local pgvector Postgres
	docker compose up -d db

check-env:     ## report what the repo .env would reach (redacted)
	@python3 scripts/check-env.py . || true   # exits 1 on a production value; the report is the point here

dev: db        ## run the API against the local db (vigil-serve handles the Windows event-loop policy)
	$(SAFE) uv run vigil-serve

test:          ## unit tests only (no services needed)
	uv run pytest

test-all: db   ## unit + integration (needs Postgres)
	uv run pytest -m ""

lint:
	uv run ruff check .

seed: db       ## apply migrations, load catalog, ingest+embed runbooks, plant deploys
	$(SAFE) uv run vigil-sim seed

demo: db       ## end-to-end demo incident (<2 min). Needs `make dev` in another terminal.
	$(SAFE) uv run vigil-sim demo --scenario bad_deploy
