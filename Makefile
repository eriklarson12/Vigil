# Vigil dev targets (require: uv, docker)
.PHONY: dev db test test-all lint seed demo

db:            ## start local pgvector Postgres
	docker compose up -d db

dev: db        ## run the API (vigil-serve handles the Windows event-loop policy)
	uv run vigil-serve

test:          ## unit tests only (no services needed)
	uv run pytest

test-all: db   ## unit + integration (needs Postgres)
	uv run pytest -m ""

lint:
	uv run ruff check .

seed:          ## apply migrations, load catalog, ingest+embed runbooks, plant deploys
	uv run vigil-sim seed

demo: db       ## end-to-end demo incident (<2 min). Needs `make dev` in another terminal.
	uv run vigil-sim demo --scenario bad_deploy
