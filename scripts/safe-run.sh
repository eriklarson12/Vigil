#!/usr/bin/env bash
# Run a Vigil entrypoint against local-only infrastructure.
#
# The repo-root .env points at production. pydantic-settings gives real
# environment variables priority over the .env file, so exporting these
# overrides them without touching the file.
#
# Usage:
#   safe-run.sh uv run vigil-sim demo --scenario bad_deploy
#   safe-run.sh --llm-live uv run vigil-sim demo --scenario bad_deploy
#   safe-run.sh --check uv run vigil-serve      # print the env, run nothing
#
# Flags opt into ONE live dependency at a time. There is deliberately no flag
# for the production database or real Slack: do those by hand, on purpose.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LLM_LIVE=0 EMB_LIVE=0 GH_LIVE=0 DRY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --llm-live)        LLM_LIVE=1; shift ;;
    --embeddings-live) EMB_LIVE=1; shift ;;
    --github-live)     GH_LIVE=1; shift ;;
    --check)           DRY=1; shift ;;
    --)                shift; break ;;
    -*)                echo "safe-run: unknown flag $1" >&2; exit 2 ;;
    *)                 break ;;
  esac
done

if [[ $# -eq 0 && $DRY -eq 0 ]]; then
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
fi

# --- always local, never overridable by a flag ---
export DATABASE_URL="postgresql://vigil:vigil@localhost:5433/vigil"
export SLACK_MODE="mock"
export SLACK_WEBHOOK_URL=""
export SLACK_BOT_TOKEN=""
export SLACK_SIGNING_SECRET=""
export ALERTMANAGER_WEBHOOK_TOKEN="dev-token"
export RESUME_TOKEN="dev-token"
export SERVICES_FILE="$ROOT/services.yaml"
export GITHUB_FIXTURES_DIR="$ROOT/tests/fixtures/github"
export LLM_FIXTURES_DIR="$ROOT/tests/fixtures/llm"

# --- opt-in live dependencies ---
if [[ $LLM_LIVE -eq 1 ]]; then export LLM_MODE="gemini"; else export LLM_MODE="fake"; fi
if [[ $EMB_LIVE -eq 1 ]]; then export EMBEDDINGS_MODE="gemini"; else export EMBEDDINGS_MODE="fake"; fi
if [[ $GH_LIVE -eq 1 ]]; then export GITHUB_MODE="live"; else export GITHUB_MODE="fixture"; export GITHUB_TOKEN=""; fi
if [[ $LLM_LIVE -eq 0 && $EMB_LIVE -eq 0 ]]; then export GEMINI_API_KEY=""; fi

# --- warn if the local database is not up ---
if ! (exec 3<>/dev/tcp/localhost/5433) 2>/dev/null; then
  echo "safe-run: nothing is listening on localhost:5433. Run 'make db' first." >&2
fi

{
  echo "safe-run: db=localhost:5433  slack=mock  llm=$LLM_MODE  embeddings=$EMBEDDINGS_MODE  github=$GITHUB_MODE"
  [[ $LLM_LIVE -eq 1 || $EMB_LIVE -eq 1 ]] && echo "safe-run: SPENDING REAL GEMINI QUOTA (llm_live=$LLM_LIVE embeddings_live=$EMB_LIVE)"
  [[ $GH_LIVE -eq 1 ]] && echo "safe-run: calling the real GitHub API"
} >&2

if [[ $DRY -eq 1 ]]; then
  echo "safe-run: --check, not running: ${*:-<no command>}" >&2
  exit 0
fi

exec "$@"
