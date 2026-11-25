<div align="center">

# Vigil

**An autonomous incident responder: production alert to Slack brief in under a minute.**

[![CI](https://github.com/eriklarson12/Vigil/actions/workflows/ci.yml/badge.svg)](https://github.com/eriklarson12/Vigil/actions/workflows/ci.yml)
[![Live dashboard](https://img.shields.io/badge/demo-live%20dashboard-4D8DFF)](https://tryvigil.vercel.app)
[![Tests](https://img.shields.io/badge/tests-463%20passing-34D399)](#development--testing)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](https://react.dev/)

[**Live dashboard →**](https://tryvigil.vercel.app) · [API health](https://vigil-app.yellowpond-d0a0dfde.eastus.azurecontainerapps.io/healthz)

</div>

---

An Alertmanager webhook fires and Vigil takes over: it scores every recent commit against the failing service, retrieves the matching runbook with hybrid RAG, classifies severity and blast radius from a service catalog, and posts a structured Block Kit brief to the on-call channel. Mark the incident resolved, in Slack or over the API, and it writes the blameless postmortem from the recorded timeline.

The whole thing runs on free tiers at $0/month: Gemini free tier, Neon Postgres, Azure Container Apps with scale-to-zero, GitHub Actions, Vercel. Clone it and the full pipeline runs offline with no API keys at all.

![Vigil incident detail: a SEV1 ConnectionPoolExhausted incident on payments-db, with four scored commit candidates. Each candidate shows a stacked contribution bar broken into recency, path match, risky files, diff size, message, and deploy window, and the top-ranked commit is tagged likely culprit at 0.809. A timeline panel on the right lists brief_posted, triage_finalized, resolved, and postmortem_posted.](assets/incident-detail.png)

<!-- The R4 demo GIF lands beside these as assets/demo.gif. Not docs/assets: docs/ is gitignored, so the image would 404 on GitHub. -->

## The problem

The first ten minutes of an incident go to gathering context: what changed, who owns this, where is the runbook, how bad is it. Vigil does that gathering in seconds, so on-call starts at the "act" step instead of the "search" step.

## Engineering Highlights

- **Three LLM calls per incident, maximum.** Deterministic scoring ranks every commit on six features (recency, path match, risky files, diff size, message signals, deploy correlation) before the model sees anything, and re-ranking is folded into the brief call. What remains is ranking, brief, and postmortem, which keeps an alert storm inside a free tier.
- **The model never decides anything consequential.** SEV1 through SEV4 come from a rules table over the service catalog (tier, user-facing, dependency fan-out) plus a BFS blast radius. A commit matching no path and no deploy window is scaled by 0.3, and nothing below the score floor is offered as a culprit: `cert_expiry` proves the pipeline reports "no likely culprit identified" instead.
- **The brief always posts.** Every node degrades on its own: no commits, empty retrieval, model down, budget exhausted. If all three LLM calls fail, a deterministic Block Kit brief ships from the heuristics alone, carrying severity, the culprit and its confidence, a runbook excerpt, and a "Mark resolved" button. A brief with gaps beats silence at 3am.
- **The fix is one click away, and a human makes it.** When the culprit clears 0.65 confidence and the verdict says to undo it, the brief adds a "Propose revert PR" button. The click is the approval. Vigil builds the revert on top of the current default branch through GitHub's Git Data API, refuses merge commits and any file changed since the culprit, and opens a PR it never merges. It writes with a separate token, so the read-only one never gains write access, and a retried click resumes its own branch instead of opening a second PR.
- **The postmortem writes itself.** Resolving an incident, in Slack or over the API, starts a second graph that reads the timeline the pipeline already recorded and posts a blameless write-up in the brief's own thread.
- **Action items become issues, exactly once.** Each postmortem action item is filed as a labeled GitHub issue. Filing runs after the postmortem graph, so the resume tick can finish a run a killed container left behind. Each URL is stored the moment it exists, and a hidden marker in every issue body lets a retry adopt an issue whose URL never reached Postgres instead of filing it twice.
- **Detection feeds the same pipeline as alerts.** With `ANOMALY_DETECTION=on`, each resume tick runs a z-score check over per-service rpm and error-rate series: a 3 hour baseline, and three consecutive points above 3 sigma. A hit becomes an Alertmanager-shaped `AnomalyDetected` alert that goes through the webhook's own ingest function, so dedup, grouping, triage, and the brief are unchanged. The alert's start time is the first point of the anomalous run, so the next tick over the same spike dedups instead of paging again. No model call is involved.
- **Retrieval starts from the link the alert already carries.** When an alert's `runbook_url` names a known runbook, that runbook leads the brief without a search. The remaining slots come from vector and full-text results fused with RRF, boosted when the runbook is tagged for the failing service. On the eleven scenarios the fusion ties either arm alone, and the [ablation](#retrieval-quality) says so.
- **Postgres is the queue.** `FOR UPDATE SKIP LOCKED` with a stale-claim reclaim after 10 minutes handles dozens of alerts a day without Kafka, Celery, or Redis. The same database holds vectors, checkpoints, and the LLM budget, so the entire system is one managed dependency.
- **Scale-to-zero survives mid-incident death.** A GitHub Actions cron POSTs `/internal/resume` every 15 minutes; the request itself wakes the container, and the tick reclaims stranded triage runs, resumes checkpointed graphs, generates missing postmortems, and prunes old rows to stay inside the 0.5 GB free tier.
- **Eleven scenarios, entirely offline.** `vigil-sim` fires Alertmanager-format alerts with planted culprits, so the whole pipeline is demonstrable and regression-testable with no API key, no network, and no live model calls.

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

`vigil-sim list` shows recent incidents and `vigil-sim delete <id>` removes one along with its
alerts, timeline, candidates, postmortem, and graph checkpoints. Deletion is irreversible and
needs the operator bearer token, so it is useful for clearing a bad demo run off the dashboard.

To see detection open an incident with no alert at all, start the server with
`ANOMALY_DETECTION=on`, write three hours of metrics with an error-rate spike, and run one tick:

```bash
uv run vigil-sim metrics --inject-spike checkout
curl -X POST -H "Authorization: Bearer dev-token" localhost:8000/internal/resume
```

## Demo scenarios

Eleven scenarios ship with the simulator, each with its own commit history, deploy events, and
alert. `--scenario <name>` is the only thing that changes between runs.

Every planted culprit in the table below is a real commit in
[`vigil-demo-shop`](https://github.com/eriklarson12/vigil-demo-shop), a public repository whose history reproduces these fixtures, so the
culprit link in a brief opens the diff that caused the incident.

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
| `ambiguous_latency` | LatencyBudgetBurn, checkout, SEV2 | **three plausible, none proven** | a measured false culprit (see Evaluation) |

The last five are where the design shows. `partial_revert` ranks an older commit above the fresher
one on top of it, because that fresher one is a `Revert` naming it, merged but never deployed.
`shared_db_saturation` and `auth_key_rotation` alert on services no user touches and still page
correctly, naming the user-facing dependents from the service graph. `cert_expiry` and
`ambiguous_latency` are the negative cases. In `cert_expiry` no commit clears the scorer's floor,
so the model is never asked, and the brief says no culprit was found and cites a runbook.
`ambiguous_latency` is where Vigil gets it wrong: the scorer cannot separate three candidates, and
the live model names one at 0.85 confidence in every recorded run, well above the 0.4 floor meant
to catch an overconfident answer. The demo replays that answer.

## Dashboard

A read-only React view over the endpoints the API already served ([`frontend/`](frontend/)):

- **Incident list:** severity, service, status, duration, and postmortem indicator, polled every 10 seconds
- **Commit candidates:** every candidate's feature scores render as a stacked contribution bar on a shared 0 to 1 scale, so you can see *why* one commit outranked the rest; expanding a row shows the raw numbers, the model's rationale, and the changed files, and candidates that failed the relevance gate are marked `gated ×0.3`
- **Slack brief:** the exact Block Kit payload that was posted, rendered in the browser
- **Postmortem:** the generated markdown
- **Stats:** MTTA (alert to brief) and MTTR (alert to resolved) at p50 and p90, split by severity, with an eight-week trend of median time to brief. Alongside them the numbers that keep the rest of this README honest: how often triage names a culprit rather than shrugging, which nodes degraded, and how many model calls an incident actually costs against the ceiling of three

![Stats page: MTTA p50 of 23 seconds and MTTR p50 of 24 seconds over fifteen resolved incidents, a triage-quality panel reporting a culprit named in 87 percent of incidents with zero degraded runs, a model-spend panel showing 2.93 calls per incident against a ceiling of three, an eight-week median-time-to-brief bar chart, and a per-severity breakdown table.](assets/stats.png)

Try `--scenario cert_expiry` to see the state where nothing scores above the floor and Vigil says so instead of guessing.

## Configuration

Every value is an environment variable; nothing is hardcoded. Defaults run the full pipeline offline.

<details>
<summary><b>Backend (<code>.env</code>)</b>, 21 variables</summary>

| Variable | Required | Description |
|---|:--:|---|
| `DATABASE_URL` | ✅ | Postgres with pgvector. Local default is `postgresql://vigil:vigil@localhost:5433/vigil`; production uses the Neon pooled URL |
| `ALERTMANAGER_WEBHOOK_TOKEN` | ✅ | Bearer token for `POST /webhooks/alertmanager` |
| `RESUME_TOKEN` | ✅ | Bearer token for the operator endpoints (resume tick, manual resolve, delete) |
| `GEMINI_API_KEY` | | From [AI Studio](https://aistudio.google.com/apikey), free and no card. Leave empty to run on fixtures |
| `LLM_MODE` | | `auto` (default), `gemini`, or `fake` |
| `GEMINI_MODEL` | | Generation model (default `gemini-3.6-flash`) |
| `GEMINI_FALLBACK_MODEL` | | Used when the primary model's quota is exhausted (default `gemini-3.5-flash-lite`) |
| `LLM_DAILY_BUDGET` | | Generation calls per Pacific day, hard stop (default `20`, the primary model's free-tier daily quota) |
| `EMBEDDING_MODEL` | | Embedding model (default `gemini-embedding-001`) |
| `EMBEDDING_DIMS` | | Matryoshka truncation width, L2-normalized in code (default `768`) |
| `EMBEDDINGS_MODE` | | `auto` (default), `gemini`, or `fake` (deterministic hash vectors) |
| `GITHUB_MODE` | | `fixture` (default, offline) or `live` |
| `GITHUB_TOKEN` | | Fine-grained read-only PAT, only for `GITHUB_MODE=live` |
| `GITHUB_DEPLOY_ENVIRONMENT` | | Deployments filtered to this environment (default `production`, empty for all) |
| `ROLLBACK_MODE` | | `mock` (default, records the would-be revert PR) or `live` |
| `GITHUB_WRITE_TOKEN` | | Fine-grained PAT with Contents, Pull requests and Issues read-write, only for `ROLLBACK_MODE=live` or `ISSUES_MODE=live` |
| `DEMO_REPO_DEFAULT_BRANCH` | | Branch revert PRs are built on and target (default `main`) |
| `ISSUES_MODE` | | `mock` (default, records the would-be issues) or `live` |
| `ISSUES_REPO` | | Repo action-item issues are filed in; blank means the incident service's repo from `services.yaml` |
| `SLACK_MODE` | | `mock` (default, console plus dashboard) or `webhook` |
| `SLACK_WEBHOOK_URL` | | Incoming webhook, required when `SLACK_MODE=webhook` |
| `SLACK_BOT_TOKEN` | | Enables `chat.postMessage` and threaded postmortems |
| `SLACK_CHANNEL` | | Target channel for the bot token (default `#incidents`) |
| `SLACK_SIGNING_SECRET` | | Verifies the brief's button requests |
| `DASHBOARD_URL` | | CORS allowlist entry and the target of the brief's Dashboard button |
| `SERVICES_FILE` | | Service catalog path (default `services.yaml`) |
| `COMMIT_LOOKBACK_HOURS` | | Commit window scored per incident (default `48`) |
| `RATE_LIMIT_PER_MIN` | | Requests per minute per sender on the webhook and Slack routes before a `429` (default `60`) |
| `ANOMALY_DETECTION` | | `on` runs the z-score detector over `metric_points` on each resume tick (default `off`) |

</details>

<details>
<summary><b>Dashboard (<code>frontend/.env.local</code>)</b></summary>

| Variable | Description |
|---|---|
| `VITE_API_URL` | API base URL, e.g. `http://localhost:8000`. Build-time, so changing it needs a redeploy |
| `VITE_UMAMI_WEBSITE_ID` | Optional. Umami website ID for product events; unset, no third-party script loads. Pageviews and Core Web Vitals go to Vercel Analytics in production builds |

</details>

## Development & Testing

```bash
# Backend: 324 unit tests, plus suites that need the database container
uv run pytest                     # unit only, no services
uv run pytest -m integration      # 65 tests against real Postgres, incl. the full pipeline
uv run pytest -m retrieval_live   # 6 retrieval-quality tests against recorded embeddings
uv run ruff check .

# Dashboard: 68 tests
cd frontend
npm run test -- --run
npm run lint
npm run typecheck
npm run build
```

GitHub Actions runs all of it on every push and pull request, with no API key and no live model calls anywhere.

Several suites exist for failures this system could otherwise hide:

- **Scoring is pinned.** Golden tests carry hand-computed feature scores. Every scenario's planted culprit must rank first, `cert_expiry` must rank nothing above the floor, and `ambiguous_latency` must leave three candidates the scorer cannot separate.
- **Fixtures cannot drift apart quietly.** The ranking node drops verdicts naming an unknown sha, so a commit fixture and its recorded LLM response falling out of sync would report "no culprit found" rather than fail. A cross-check asserts they match, every fixture must declare its culprit or be listed as deliberately having none, and recorded responses are parsed through the real Pydantic schemas so prompt drift breaks CI.
- **Resume is tested by killing it.** A chaos test kills the triage graph while it is parked inside the ranking call, resumes it through the stale-claim reclaim a restarted container uses, and asserts one brief posted, two model calls charged, and the end state of a run that was never interrupted.
- **Redaction is tested against the real leak.** An `httpx.HTTPStatusError` from a failed Slack post carries the webhook URL, which is itself the credential, into the degradation log line. A structlog processor at the head of the chain strips it.

## API Reference

<details>
<summary><b>Ten endpoints</b>: ingest, Slack interactions and slash command, dashboard reads, operator actions, cron tick</summary>

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/alertmanager` | Alertmanager v4 payload: validate, fingerprint, group, enqueue (bearer token) |
| `POST` | `/slack/interactions` | Slack "Mark resolved" and "Propose revert PR" buttons, signature-verified |
| `POST` | `/slack/commands` | `/vigil status` lists open incidents, `/vigil resolve <id>` resolves one; signature-verified, ephemeral replies |
| `GET` | `/api/incidents` | Incident list for the dashboard |
| `GET` | `/api/incidents/{id}` | One incident with candidates, runbook, brief, timeline, and postmortem |
| `GET` | `/api/stats` | MTTA, MTTR, triage quality, and model spend, computed on read |
| `GET` | `/api/metrics/{service}` | The last 24 hours of the service's rpm and error-rate points |
| `POST` | `/api/incidents/{id}/resolve` | Manual resolve, starts the postmortem graph (bearer token) |
| `DELETE` | `/api/incidents/{id}` | Hard-delete one incident with its alerts, timeline, candidates, postmortem, and graph checkpoints (bearer token) |
| `POST` | `/internal/resume` | Cron tick: detect anomalies, reclaim stale work, resume checkpoints, finish stranded revert PRs and action-item issues, prune (bearer token) |
| `GET` | `/healthz` | Liveness check, and the request that wakes a scaled-to-zero container |

</details>

## Evaluation

Commit ranking is measured against the live model, not the recorded responses CI replays. `scripts/eval_ranking.py` builds the production ranking prompt for each of the 11 synthetic scenarios (4 to 5 commits in the window each, of which the 2 or 3 that clear the heuristic floor reach the model) and sends it 3 times. Measured with `gemini-3.6-flash` on 2026-11-24 and 2026-11-25; the free tier's 20 requests a day split the 30 calls across both days.

| | Heuristic only | Heuristic + model |
|---|---|---|
| Planted culprit named, 9 scenarios | 9/9 | 24/27 runs (8/9 scenarios) |
| False culprit, 2 scenarios with none | 1/2 | 3/3 runs on the 1 that reaches the model |
| Same answer in all 3 runs | n/a | 10/10 scenarios |

What these numbers do and do not show:

- The heuristic weights were written against these same scenarios, so 9/9 shows they fit them. It says nothing about real repositories.
- The model sees each candidate's heuristic score. It agreed with the heuristic's first pick everywhere except `dependency_bump`, where it blamed a retry decorator in the orders HTTP client over the `httpx` upgrade, at 0.90 confidence in all 3 runs.
- `cert_expiry` has no candidate above the floor, so no call is made. `ambiguous_latency` has no culprit either, and the model names the batching commit at 0.85 every run, so its demo brief blames that commit.
- On this evidence the model adds a written rationale per commit, not accuracy. Every scenario was written by the author of the scorer.

Reproduce with `./scripts/safe-run.sh --llm-live uv run python scripts/eval_ranking.py`, adding `--fill` on the next day to finish runs the daily quota cut off. Per-run answers and rationales are in `tests/fixtures/eval/ranking.json`.

## Retrieval quality

Runbook retrieval is regression-tested against real embeddings without spending a single API call. Recorded Gemini vectors for every runbook chunk and every scenario query are committed, so CI replays them and measures retrieval itself rather than the SQL plumbing around it. Recording is incremental: adding a runbook or a scenario embeds only what is new and carries the rest over untouched.

Two gates run at the production fetch depth across all eleven scenarios: `hit@3` (the right runbook reaches the brief at all) must be at least 10 of 11, and `rank@1` (it reaches the brief first) at least 9 of 11. `rank@1` exists because `hit@3` alone is blind to the failure actually observed in production, where an unrelated runbook outranked the right one while both sat in the top 3.

| Metric | Gate | Current |
|---|---|---|
| `hit@3` | 10/11 | **11/11** |
| `rank@1` | 9/11 | **10/11** |

The scenario alerts are written the way an Alertmanager rule would template them: a metric, a threshold, a duration, and the route or pod affected. They never name a changed file, a config key, a deploy, or a cause, and `tests/unit/test_scenario_hygiene.py` fails the build if one does. Before that rewrite on 2026-11-24, several annotations stated the diagnosis outright and `rank@1` was 11/11. Since it, `dependency_bump` ranks its runbook second, behind `orders-database`.

The same test prints both numbers for each arm alone, for the fusion without the tag boost, and for production. Only the production row is gated.

| Configuration | `hit@3` | `rank@1` | Not ranked first |
|---|---|---|---|
| Vector only | 11/11 | 10/11 | `dependency_bump` (3rd) |
| Keyword only | 11/11 | 10/11 | `partial_revert` (2nd) |
| Hybrid (RRF) | 11/11 | 10/11 | `dependency_bump` (2nd) |
| Hybrid + tag boost (production) | 11/11 | 10/11 | `dependency_bump` (2nd) |

Hybrid does not beat vector-only here. The arms miss different scenarios: the fusion gets `partial_revert` right where the keyword arm alone does not, and lifts `dependency_bump` from third to second, but neither headline number moves. The tag boost changes nothing, because five of the seven runbooks are tagged `checkout`. Eleven queries over seven runbooks cannot separate these configurations. They show the fusion costs nothing on this corpus. They do not show it helps.

Alertmanager rules usually carry a `runbook_url` annotation, and on a corpus this small that link does more than any ranking. When its last path segment equals a runbook slug, that runbook's chunk nearest the query is pinned first with `source: "annotation"`, and retrieval fills the other slots under the same cap of 4 chunks from 2 runbooks. A missing or unknown URL changes nothing. `cert_expiry` carries one. The tables above score retrieval alone, so the pin does not count toward them.

Reproduce with `docker compose up -d db && uv run pytest -m retrieval_live -s`, which prints the ranked runbooks per scenario.

## Deployment

Live on Azure Container Apps at [`/healthz`](https://vigil-app.yellowpond-d0a0dfde.eastus.azurecontainerapps.io/healthz), with the dashboard on Vercel at [tryvigil.vercel.app](https://tryvigil.vercel.app).

- **API → Azure Container Apps**, scale-to-zero with a maximum of one replica. GitHub Actions builds the image, pushes to ACR, and rolls it out; authentication is OIDC through a federated credential, so no long-lived Azure secrets are stored in the repo.
- **Database → Neon**, free tier with pgvector. Migrations apply on app startup.
- **Dashboard → Vercel**, root directory `frontend/`, with `VITE_API_URL` pointed at the container app.
- Set `DASHBOARD_URL` on the API to the Vercel origin: it is both the CORS allowlist entry and the target of the brief's Dashboard button.
- The webhook and both Slack routes answer `429` with `Retry-After` past `RATE_LIMIT_PER_MIN`, counted per authenticated sender after the token or signature check.
- A scheduled workflow POSTs `/internal/resume` every 15 minutes, which is what makes scale-to-zero safe.

## Limitations

- **The demo data is synthetic.** Alerts come from `vigil-sim` and commit history replays from fixtures, so deployed incidents are reproducible rather than real. The commits are genuine: the fixtures are built into [a public repo](https://github.com/eriklarson12/vigil-demo-shop) and read back through the live GitHub client, so every culprit link opens the planted diff. Production stays on fixtures by design, since a live fetch only looks back 48 hours.
- **The dashboard is read-only and unauthenticated.** That is deliberate for synthetic data. Add authentication before pointing it at anything real; the state-changing endpoints already require a bearer token.
- **Free-tier quotas are a hard ceiling.** A Postgres-backed daily budget stops generation calls at `LLM_DAILY_BUDGET`, and Vigil falls back to the lighter model and then to the deterministic brief rather than queueing spend.
- **The first request after idle is slow.** Scale-to-zero plus a Neon cold start means a few seconds on the first hit, which is the cost of the $0 budget.
- **One replica.** The `SKIP LOCKED` queue is built for more, but the free grant is not, so throughput is bounded by a single container. The rate-limit windows and the per-incident triage lock live in process memory for the same reason; a second replica would need both moved into Postgres.
- **Storage is pruned, not archived.** The Neon free tier is 0.5 GB, so the resume tick trims old incidents and checkpoints on a schedule.

---

Built by [Erik Larson](https://github.com/eriklarson12). The incidents in the live demo are simulated; no production system is being watched.
