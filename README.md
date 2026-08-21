<div align="center">

# Vigil

**An autonomous incident responder: production alert to Slack brief in under a minute.**

[![CI](https://github.com/eriklarson12/Vigil/actions/workflows/ci.yml/badge.svg)](https://github.com/eriklarson12/Vigil/actions/workflows/ci.yml)
[![Live dashboard](https://img.shields.io/badge/demo-live%20dashboard-4D8DFF)](https://vigil-silk-nine.vercel.app)
[![Tests](https://img.shields.io/badge/tests-150%20passing-34D399)](#development--testing)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev/)

[**Live dashboard →**](https://vigil-silk-nine.vercel.app) · [API health](https://vigil-app.yellowpond-d0a0dfde.eastus.azurecontainerapps.io/healthz)

</div>

---

An Alertmanager webhook fires and Vigil takes over: it scores every recent commit against the failing service, retrieves the matching runbook with hybrid RAG, classifies severity and blast radius from a service catalog, and posts a structured Block Kit brief to the on-call channel. Mark the incident resolved, in Slack or over the API, and it writes the blameless postmortem from the recorded timeline.

The whole thing runs on free tiers at $0/month: Gemini free tier, Neon Postgres, Azure Container Apps with scale-to-zero, GitHub Actions, Vercel. Clone it and the full pipeline runs offline with no API keys at all.

<!-- Screenshot: dashboard incident detail with the commit-candidate contribution bars. Add as assets/dashboard.png (width 900, alt text describing the panel). -->

## Table of Contents

- [The problem](#the-problem)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Getting Started](#getting-started)
- [Demo scenarios](#demo-scenarios)
- [Dashboard](#dashboard)
- [Configuration](#configuration)
- [Development & Testing](#development--testing)
- [API Reference](#api-reference)
- [Retrieval quality](#retrieval-quality)
- [Engineering Highlights](#engineering-highlights)
- [Deployment](#deployment)
- [Limitations](#limitations)

## The problem

The first ten minutes of an incident go to gathering context: what changed, who owns this, where is the runbook, how bad is it. Vigil does that gathering in seconds, so on-call starts at the "act" step instead of the "search" step.

## Features

- **Bad-commit identification:** deterministic scoring ranks every commit in the lookback window on six features (recency, path match, risky files, diff size, message signals, deploy correlation) before the LLM sees anything
- **Runbook retrieval:** pgvector cosine similarity and Postgres full-text search, fused with reciprocal rank fusion and boosted when the runbook is tagged for the failing service
- **Deterministic severity:** SEV1 through SEV4 come from a rules table over the service catalog (tier, user-facing, dependency fan-out) plus a BFS blast radius, never from the model
- **Slack brief:** Block Kit message with severity, culprit commit and confidence, runbook excerpt, affected services, and a "Mark resolved" button
- **Automatic postmortem:** resolution starts a second graph that gathers the incident timeline and posts a blameless postmortem in the brief's thread
- **Incident dashboard:** read-only React view of every incident, including the per-feature breakdown of why one commit outranked the rest
- **Incident simulator:** `vigil-sim` fires Alertmanager-format alerts for eleven scenarios with planted culprits, entirely offline
- **Exactly-once resumability:** LangGraph checkpoints live in Postgres, so a container killed mid-triage resumes without duplicate LLM spend

## Tech Stack

| Layer | Technology |
|---|---|
| API | Python 3.12, FastAPI, Pydantic v2, psycopg 3, structlog |
| Orchestration | LangGraph with the Postgres checkpointer |
| AI | Gemini (`google-genai`) for commit ranking, brief, and postmortem; `gemini-embedding-001` at 768 dims for retrieval |
| Data | Neon Postgres with pgvector: state, work queue, vectors, checkpoints, budget |
| Dashboard | React 19, TypeScript, Vite, Tailwind CSS v4 |
| Integrations | Alertmanager webhooks, Slack Block Kit and interactions, GitHub REST |
| Testing | pytest, ruff, Vitest, oxlint |
| Infrastructure | Azure Container Apps (scale-to-zero), Vercel, GitHub Actions |

## Architecture

```mermaid
flowchart LR
    AM[Alertmanager / vigil-sim] -->|webhook| API
    subgraph ACA[Azure Container Apps: scale-to-zero]
        API[FastAPI · LangGraph pipeline]
    end
    API <-->|state · queue · vectors · checkpoints| PG[(Neon Postgres + pgvector)]
    API -->|3 calls max per incident| GEM[Gemini free tier]
    API --> GH[GitHub REST API]
    API <--> SLACK[Slack]
    DASH[React dashboard on Vercel] -->|REST| API
    TICK[GitHub Actions cron, 15 min] -->|wakes app| API
```

Every inbound signal is an HTTP request and every piece of state lives in Postgres. The container is killed routinely by scale-to-zero, and an in-flight incident resumes from its LangGraph checkpoint exactly once, with no duplicate LLM spend.

Triage runs the three expensive lookups in parallel and joins them into a single brief call:

```
START → load_context ─┬→ fetch_commits → score_commits → rank_commits_llm ─┐
                      ├→ retrieve_runbooks ────────────────────────────────┤
                      └→ estimate_impact ──────────────────────────────────┤
                                                     compose_brief (join) ←┘
                                                     → post_slack → finalize → END
```

## Getting Started

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) for the local pgvector Postgres
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Node.js 22+ (dashboard only)
- No API keys. The defaults run fully offline: `LLM_MODE=auto` falls back to recorded fixtures, `GITHUB_MODE=fixture` replays commit history, `SLACK_MODE=mock` prints the brief instead of posting it.

### Installation

```bash
git clone https://github.com/eriklarson12/Vigil.git
cd Vigil
cp .env.example .env
docker compose up -d db     # pgvector Postgres on localhost:5433
uv sync
```

### Usage

```bash
uv run vigil-serve                              # terminal 1: API on :8000
uv run vigil-sim demo --scenario bad_deploy     # terminal 2: full incident in ~15s
cd frontend && npm install && npm run dev       # terminal 3: dashboard on :5173
```

The demo seeds and embeds the runbooks, plants the scenario's deploy events, fires the alert, waits for the brief, resolves the incident, and prints the generated postmortem. Open <http://localhost:5173> to see the same incident in the dashboard.

`make demo` runs the same thing, and `make test-all` runs every test suite against the local database.

## Demo scenarios

Eleven scenarios ship with the simulator, each with its own commit history, deploy events, and
alert. `--scenario <name>` is the only thing that changes between runs.

```bash
uv run vigil-sim demo --scenario shared_db_saturation
```

| Scenario | Alert | Planted culprit | Shows off |
|---|---|---|---|
| `bad_deploy` | HighErrorRate, checkout, SEV1 | validation removed in a refactor | deploy correlation |
| `db_migration_lock` | SlowQueries, orders, SEV2 | non-CONCURRENT index build | risky-file scoring |
| `memory_leak` | HighMemory, inventory, SEV4 | unbounded cache, 30h old | time decay vs path match |
| `config_typo` | CrashLoopBackOff, checkout, SEV1 | one-character YAML change | tiny-diff risk weighting |
| `dependency_bump` | HighLatency, orders, SEV2 | lockfile bump | dependency heuristics |
| `hotfix_regression` | OrderQueryTimeouts, orders, SEV2 | 6-line hotfix disabling a timeout | message signals beating diff size |
| `partial_revert` | PaymentFailures, checkout, SEV1 | pricing change whose revert is merged but undeployed | a teammate's revert as evidence |
| `shared_db_saturation` | ConnectionPoolExhausted, payments-db, SEV1 | statement timeout raised to 60s | blast radius through a shared dependency |
| `auth_key_rotation` | TokenValidationFailures, auth, SEV3 | JWKS rotation that drops the old key | internal service, user-facing fan-out |
| `cert_expiry` | TLSHandshakeErrors, checkout, SEV1 | **none exists** | the honest "no culprit found" path |
| `ambiguous_latency` | LatencyBudgetBurn, checkout, SEV2 | **three plausible, none proven** | declining to name a culprit |

The last five are where the design shows. `partial_revert` ranks a two-hour-old commit above the
fresher change on top of it, because that fresher change is a `Revert` naming it, merged but never
deployed. `shared_db_saturation` and `auth_key_rotation` alert on services no user touches and
still page correctly, with the user-facing dependents named from the service graph. `cert_expiry`
has no culprit to find, and in `ambiguous_latency` the drift predates every change in the window
and the payments provider reports its own latency regression, so the three candidates stay ranked
and unaccused: the brief names no culprit, keeps every rationale, and cites the dashboards runbook
instead of inventing a root cause. The 0.4 confidence floor is the backstop when the model is less
sure of that than it should be.

## Dashboard

A read-only React view over the same two endpoints the API already served ([`frontend/`](frontend/)):

- **Incident list:** severity, service, status, duration, and postmortem indicator, polled every 10 seconds
- **Commit candidates:** every candidate's feature scores render as a stacked contribution bar on a shared 0 to 1 scale, so you can see *why* one commit outranked the rest; expanding a row shows the raw numbers, the model's rationale, and the changed files, and candidates that failed the relevance gate are marked `gated ×0.3`
- **Slack brief:** the exact Block Kit payload that was posted, rendered in the browser
- **Postmortem:** the generated markdown

Try `--scenario cert_expiry` to see the state where nothing scores above the floor and Vigil says so instead of guessing.

## Configuration

### Backend (`.env`)

| Variable | Required | Description |
|---|:--:|---|
| `DATABASE_URL` | ✅ | Postgres with pgvector. Local default is `postgresql://vigil:vigil@localhost:5433/vigil`; production uses the Neon pooled URL |
| `ALERTMANAGER_WEBHOOK_TOKEN` | ✅ | Bearer token for `POST /webhooks/alertmanager` |
| `RESUME_TOKEN` | ✅ | Bearer token for the operator endpoints (resume tick, manual resolve) |
| `GEMINI_API_KEY` | | From [AI Studio](https://aistudio.google.com/apikey), free and no card. Leave empty to run on fixtures |
| `LLM_MODE` | | `auto` (default), `gemini`, or `fake` |
| `GEMINI_MODEL` | | Generation model (default `gemini-3.6-flash`) |
| `GEMINI_FALLBACK_MODEL` | | Used when the primary model's quota is exhausted (default `gemini-3.5-flash-lite`) |
| `LLM_DAILY_BUDGET` | | Generation calls per day, hard stop (default `200`) |
| `EMBEDDING_MODEL` | | Embedding model (default `gemini-embedding-001`) |
| `EMBEDDING_DIMS` | | Matryoshka truncation width, L2-normalized in code (default `768`) |
| `EMBEDDINGS_MODE` | | `auto` (default), `gemini`, or `fake` (deterministic hash vectors) |
| `GITHUB_MODE` | | `fixture` (default, offline) or `live` |
| `GITHUB_TOKEN` | | Fine-grained read-only PAT, only for `GITHUB_MODE=live` |
| `SLACK_MODE` | | `mock` (default, console plus dashboard) or `webhook` |
| `SLACK_WEBHOOK_URL` | | Incoming webhook, required when `SLACK_MODE=webhook` |
| `SLACK_BOT_TOKEN` | | Enables `chat.postMessage` and threaded postmortems |
| `SLACK_CHANNEL` | | Target channel for the bot token (default `#incidents`) |
| `SLACK_SIGNING_SECRET` | | Verifies the "Mark resolved" button's requests |
| `DASHBOARD_URL` | | CORS allowlist entry and the target of the brief's Dashboard button |
| `SERVICES_FILE` | | Service catalog path (default `services.yaml`) |
| `COMMIT_LOOKBACK_HOURS` | | Commit window scored per incident (default `48`) |

### Dashboard (`frontend/.env.local`)

| Variable | Description |
|---|---|
| `VITE_API_URL` | API base URL, e.g. `http://localhost:8000`. Build-time, so changing it needs a redeploy |

## Development & Testing

```bash
# Backend: 93 unit tests, plus suites that need the database container
uv run pytest                     # unit only, no services
uv run pytest -m integration      # 2 full-pipeline tests against real Postgres
uv run pytest -m retrieval_live   # 4 retrieval-quality tests against recorded embeddings
uv run ruff check .

# Dashboard: 51 tests
cd frontend
npm run test -- --run
npm run lint
npm run typecheck
npm run build
```

GitHub Actions runs all of it on every push and pull request, with no API key and no live model calls anywhere.

The commit scorer has golden tests with hand-computed expected values, and each scenario's planted culprit must rank first, while `cert_expiry` must rank nothing above the score floor and `ambiguous_latency` must leave three candidates the scorer cannot separate. A test asserts that every commit fixture is covered by that culprit map, so a new fixture cannot quietly escape the assertion. LLM fixtures are parsed through the real Pydantic schemas, so prompt or schema drift fails CI loudly.

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/alertmanager` | Alertmanager v4 payload: validate, fingerprint, group, enqueue (bearer token) |
| `POST` | `/slack/interactions` | Slack "Mark resolved" button, signature-verified |
| `GET` | `/api/incidents` | Incident list for the dashboard |
| `GET` | `/api/incidents/{id}` | One incident with candidates, runbook, brief, timeline, and postmortem |
| `POST` | `/api/incidents/{id}/resolve` | Manual resolve, starts the postmortem graph (bearer token) |
| `POST` | `/internal/resume` | Cron tick: reclaim stale work, resume checkpoints, prune (bearer token) |
| `GET` | `/healthz` | Liveness check, and the request that wakes a scaled-to-zero container |

## Retrieval quality

Runbook retrieval is regression-tested against real embeddings without spending a single API call. Recorded Gemini vectors for every runbook chunk and every scenario query are committed, so CI replays them and measures retrieval itself rather than the SQL plumbing around it. Recording is incremental: adding a runbook or a scenario embeds only what is new and carries the rest over untouched.

Two gates run at the production fetch depth across all eleven scenarios: `hit@3` (the right runbook reaches the brief at all) must be at least 10 of 11, and `rank@1` (it reaches the brief first) at least 9 of 11. `rank@1` exists because `hit@3` alone is blind to the failure actually observed in production, where an unrelated runbook outranked the right one while both sat in the top 3.

| Metric | Gate | Current |
|---|---|---|
| `hit@3` | 10/11 | **11/11** |
| `rank@1` | 9/11 | **11/11** |

Reproduce with `docker compose up -d db && uv run pytest -m retrieval_live -s`, which prints the ranked runbooks per scenario.

## Engineering Highlights

- **Three LLM calls per incident, maximum.** Deterministic pre-scoring ranks commits before the model sees anything, severity comes from a rules table, and re-ranking is folded into the brief call. What remains is ranking, brief, and postmortem, which keeps an alert storm inside a free tier instead of on top of it.
- **The brief always posts.** Every pipeline node degrades on its own: no commits found, retrieval empty, model unavailable, daily budget exhausted. If all three LLM calls fail, a deterministic Block Kit brief ships from the heuristics alone, because a brief with gaps beats silence at 3am.
- **Postgres is the queue.** `FOR UPDATE SKIP LOCKED` with a stale-claim reclaim after 10 minutes handles dozens of alerts a day without Kafka, Celery, or Redis. The same database holds vectors, checkpoints, and the LLM budget, so the entire system is one managed dependency.
- **Scale-to-zero survives mid-incident death.** A GitHub Actions cron POSTs `/internal/resume` every 15 minutes; the request itself wakes the container, and the tick reclaims stranded triage runs, resumes checkpointed graphs, generates missing postmortems, and prunes old rows to stay inside the 0.5 GB free tier.
- **Hybrid retrieval, because runbooks are full of identifiers.** Runbook text is dense with exact strings such as service names, error codes, and table names, where lexical search beats embeddings. Vector and full-text results are fused with RRF and boosted when the runbook is tagged for the failing service.
- **A relevance gate that admits ignorance.** A commit touching none of the service's paths and landing in no deploy window is multiplied by 0.3, and nothing below the score floor is offered as a culprit. The `cert_expiry` scenario exists to prove the pipeline reports "no likely culprit identified" instead of promoting the highest-scoring irrelevant commit.

## Deployment

Live on Azure Container Apps at [`/healthz`](https://vigil-app.yellowpond-d0a0dfde.eastus.azurecontainerapps.io/healthz), with the dashboard on Vercel at [vigil-silk-nine.vercel.app](https://vigil-silk-nine.vercel.app).

- **API → Azure Container Apps**, scale-to-zero with a maximum of one replica. GitHub Actions builds the image, pushes to ACR, and rolls it out; authentication is OIDC through a federated credential, so no long-lived Azure secrets are stored in the repo.
- **Database → Neon**, free tier with pgvector. Migrations apply on app startup.
- **Dashboard → Vercel**, root directory `frontend/`, with `VITE_API_URL` pointed at the container app.
- Set `DASHBOARD_URL` on the API to the Vercel origin: it is both the CORS allowlist entry and the target of the brief's Dashboard button.
- A scheduled workflow POSTs `/internal/resume` every 15 minutes, which is what makes scale-to-zero safe.

## Limitations

- **The demo data is synthetic.** Alerts come from `vigil-sim` and commit history is replayed from fixtures, so the deployed incidents are reproducible rather than real. Live GitHub mode needs a public repo with matching planted commits.
- **The dashboard is read-only and unauthenticated.** That is deliberate for synthetic data. Add authentication before pointing it at anything real; the state-changing endpoints already require a bearer token.
- **Free-tier quotas are a hard ceiling.** A Postgres-backed daily budget stops generation calls at `LLM_DAILY_BUDGET`, and Vigil falls back to the lighter model and then to the deterministic brief rather than queueing spend.
- **The first request after idle is slow.** Scale-to-zero plus a Neon cold start means a few seconds on the first hit, which is the cost of the $0 budget.
- **One replica.** The `SKIP LOCKED` queue is built for more, but the free grant is not, so throughput is bounded by a single container.
- **Storage is pruned, not archived.** The Neon free tier is 0.5 GB, so the resume tick trims old incidents and checkpoints on a schedule.

---

Built by [Erik Larson](https://github.com/eriklarson12). The incidents in the live demo are simulated; no production system is being watched.
