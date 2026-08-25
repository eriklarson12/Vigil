#!/usr/bin/env python3
"""Report which production systems the repo-root .env would reach.

Usage: check-env.py [repo_root]

Prints a classification, never a secret. Exit 1 means at least one value
points at production, so no entrypoint may be run without explicit overrides.
"""
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "db", "postgres"}
live, safe, missing = [], [], []


def redact(value, keep=6):
    if not value:
        return "(empty)"
    return f"{value[:keep]}...({len(value)} chars)"


def parse_env(path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        # Quoted value wins; otherwise " #" starts an inline comment.
        # A leading '#' is data (SLACK_CHANNEL=#incidents), not a comment.
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        env[k.strip()] = v
    return env


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    r = root / ".env"
    env = parse_env(r)
    if not env:
        print(f"no .env at {r}: config.py defaults apply, which are safe")
        return 0

    db = env.get("DATABASE_URL", "")
    if db:
        host = urlparse(db).hostname or "?"
        port = urlparse(db).port or ""
        where = f"{host}:{port}" if port else host
        if host in LOCAL_HOSTS:
            safe.append(f"DATABASE_URL -> {where} (local)")
        else:
            live.append(f"DATABASE_URL -> {where}  ** PRODUCTION DATABASE **")
    else:
        missing.append("DATABASE_URL (default is local, fine)")

    slack_mode = env.get("SLACK_MODE", "mock")
    hook = env.get("SLACK_WEBHOOK_URL", "")
    bot = env.get("SLACK_BOT_TOKEN", "")
    chan = env.get("SLACK_CHANNEL", "")
    if slack_mode == "webhook" and (hook or bot):
        live.append(
            f"SLACK_MODE=webhook with credentials  ** POSTS TO REAL SLACK {chan or '(default channel)'} **"
        )
        if bot:
            live.append(f"  SLACK_BOT_TOKEN present: {redact(bot, 5)}")
        if hook:
            live.append(f"  SLACK_WEBHOOK_URL present: {redact(hook, 24)}")
    else:
        safe.append(f"SLACK_MODE={slack_mode}")

    key = env.get("GEMINI_API_KEY", "")
    for var, label in (("LLM_MODE", "generation"), ("EMBEDDINGS_MODE", "embeddings")):
        mode = env.get(var, "auto")
        if mode == "fake":
            safe.append(f"{var}=fake")
        elif key:
            live.append(f"{var}={mode} with GEMINI_API_KEY set  ** SPENDS REAL {label.upper()} QUOTA **")
        else:
            safe.append(f"{var}={mode} but no API key (falls back to fake)")

    gh = env.get("GITHUB_MODE", "fixture")
    if gh == "live":
        live.append("GITHUB_MODE=live  ** CALLS THE REAL GITHUB API **")
    else:
        safe.append(f"GITHUB_MODE={gh}")

    for tok in ("ALERTMANAGER_WEBHOOK_TOKEN", "RESUME_TOKEN"):
        v = env.get(tok, "")
        if v and v != "dev-token":
            live.append(f"{tok} is the real deployed token: {redact(v, 4)}")
        else:
            safe.append(f"{tok}={v or '(unset)'}")

    print(f"env audit: {r}\n")
    if live:
        print(f"REACHES PRODUCTION ({len(live)})")
        for x in live:
            print(f"  {x}")
        print()
    if safe:
        print("safe as configured")
        for x in safe:
            print(f"  {x}")
        print()
    if missing:
        print("not set (defaults apply)")
        for x in missing:
            print(f"  {x}")
        print()

    if live:
        print("Any process that builds Settings() outside pytest inherits the values above.")
        print("Run entrypoints through safe-run.sh, never bare.")
        return 1
    print("No production values found. Entrypoints are safe to run bare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
