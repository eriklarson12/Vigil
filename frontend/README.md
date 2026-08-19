# Vigil dashboard

Read-only React view over the Vigil API: the incident list, the commit-candidate
score breakdown, the posted Slack brief, and the generated postmortem.

## Run it

```bash
npm install
npm run dev          # http://localhost:5173
```

The API must be running separately:

```bash
docker compose up -d db                        # from the repo root
uv run vigil-serve                             # :8000
uv run vigil-sim demo --scenario bad_deploy    # produces an incident to look at
```

## Configuration

| Var | Default | Notes |
|---|---|---|
| `VITE_API_URL` | `http://localhost:8000` | Base URL of the Vigil API. Set it in `.env.local` locally, and as a Vercel project env var in production. |

The API's CORS allowlist is `DASHBOARD_URL` plus `http://localhost:5173`
(`src/vigil/main.py`), so a deployed dashboard needs `DASHBOARD_URL` set on the
container app to its own origin.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server on :5173 |
| `npm run build` | Type-check then production build into `dist/` |
| `npm run typecheck` | Types only |
| `npm run test` | Vitest: score math, formatters, and a render smoke test against captured API payloads |
| `npm run lint` | oxlint |

## Notes

- **No auth.** v1 is read-only and the data is synthetic demo data. Do not point
  it at anything real without adding authentication first.
- Polling is a plain `setInterval` every 10s (`src/api.ts`). The incident detail
  page stops polling once the postmortem lands.
- `src/scoring.ts` mirrors `WEIGHTS` and `RELEVANCE_GATE` from
  `src/vigil/commits/scoring.py`. That Python file is the source of truth; the
  golden values in `src/scoring.test.ts` are cross-checked against
  `tests/unit/test_scoring.py`.
- The Slack brief is rendered from the Block Kit payload stored on the
  `brief_posted` incident event. Slack `mrkdwn` is parsed by `parseMrkdwn` in
  `src/format.ts`, not by a markdown renderer.
- `src/__fixtures__/` holds API responses captured from a real `vigil-sim demo`
  run (`bad_deploy` and `cert_expiry`). `src/render.test.tsx` server-renders the
  panels against them, which is what catches "the API changed shape" without a
  browser. Refresh them with `curl` when the API's output changes.
