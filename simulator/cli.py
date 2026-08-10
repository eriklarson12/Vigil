"""vigil-sim — seed | fire | resolve | demo (spec §15).

Timestamps are rewritten relative to *now* at fire time so the scoring
time-decay behaves identically no matter when the demo runs.
"""

import asyncio
import json
import pathlib
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx
import typer

if sys.platform == "win32":
    # psycopg async cannot run on the ProactorEventLoop (Windows default)
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

app = typer.Typer(help="Vigil incident simulator")

SIM_DIR = pathlib.Path(__file__).parent
SCENARIOS_DIR = SIM_DIR / "scenarios"
RUNBOOKS_DIR = SIM_DIR / "runbooks"


def _load_scenario(name: str) -> dict:
    path = SCENARIOS_DIR / f"{name}.json"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in SCENARIOS_DIR.glob("*.json")))
        raise typer.BadParameter(f"unknown scenario '{name}'. Available: {available}")
    return json.loads(path.read_text(encoding="utf-8"))


def _payload(scenario: dict, status: str, starts_at: datetime, ends_at: datetime | None) -> dict:
    alert = scenario["alert"]
    return {
        "version": "4",
        "groupKey": f'{{}}:{{alertname="{alert["labels"]["alertname"]}"}}',
        "status": status,
        "receiver": "vigil",
        "groupLabels": {"alertname": alert["labels"]["alertname"]},
        "commonLabels": alert["labels"],
        "commonAnnotations": alert["annotations"],
        "externalURL": "http://alertmanager.local",
        "alerts": [
            {
                "status": status,
                "labels": alert["labels"],
                "annotations": alert["annotations"],
                "startsAt": starts_at.isoformat(),
                "endsAt": ends_at.isoformat() if ends_at else "0001-01-01T00:00:00Z",
                "fingerprint": alert["fingerprint"],
            }
        ],
    }


def _post(url: str, path: str, body: dict, token: str) -> dict:
    resp = httpx.post(
        f"{url}{path}", json=body, headers={"Authorization": f"Bearer {token}"}, timeout=30.0
    )
    resp.raise_for_status()
    return resp.json()


async def _seed_async() -> None:
    from vigil.config import get_settings
    from vigil.db.pool import apply_migrations, create_pool, open_pool_with_retry
    from vigil.rag.embed import get_embedder, ingest_runbook

    settings = get_settings()
    pool = create_pool(settings.database_url)
    await open_pool_with_retry(pool)
    await apply_migrations(pool)
    embedder = get_embedder(settings)
    for path in sorted(RUNBOOKS_DIR.glob("*.md")):
        result = await ingest_runbook(pool, embedder, path)
        typer.echo(f"  runbook {result['slug']}: {result['status']} ({result['chunks']} chunks)")
    await pool.close()


async def _plant_deploys(scenario: dict, now: datetime) -> None:
    from vigil.config import get_settings
    from vigil.db.pool import create_pool, open_pool_with_retry

    deploys = scenario.get("deploys", [])
    if not deploys:
        return
    settings = get_settings()
    pool = create_pool(settings.database_url)
    await open_pool_with_retry(pool)
    async with pool.connection() as conn:
        for d in deploys:
            finished = now - timedelta(minutes=d["minutes_before_alert"])
            await conn.execute(
                "INSERT INTO deploy_events (service, commit_shas, finished_at) VALUES (%s, %s, %s)",
                (d["service"], d["commit_shas"], finished),
            )
    await pool.close()
    typer.echo(f"  planted {len(deploys)} deploy event(s)")


@app.command()
def seed() -> None:
    """Apply migrations and ingest+embed all seed runbooks (idempotent)."""
    typer.echo("Seeding runbooks…")
    asyncio.run(_seed_async())
    typer.echo("Done.")


@app.command()
def fire(
    scenario: str = typer.Option(..., help="scenario name, e.g. bad_deploy"),
    url: str = typer.Option("http://localhost:8000"),
    token: str = typer.Option("dev-token", envvar="ALERTMANAGER_WEBHOOK_TOKEN"),
) -> None:
    """Plant the scenario's deploy events and POST its firing alert."""
    sc = _load_scenario(scenario)
    now = datetime.now(UTC)
    asyncio.run(_plant_deploys(sc, now))
    result = _post(url, "/webhooks/alertmanager", _payload(sc, "firing", now, None), token)
    typer.echo(f"fired {scenario}: {result}")


@app.command()
def resolve(
    scenario: str = typer.Option(...),
    url: str = typer.Option("http://localhost:8000"),
    token: str = typer.Option("dev-token", envvar="ALERTMANAGER_WEBHOOK_TOKEN"),
) -> None:
    """POST the matching `resolved` alert (fingerprint match resolves the incident)."""
    sc = _load_scenario(scenario)
    now = datetime.now(UTC)
    payload = _payload(sc, "resolved", now - timedelta(minutes=9), now)
    result = _post(url, "/webhooks/alertmanager", payload, token)
    typer.echo(f"resolved {scenario}: {result}")


@app.command()
def demo(
    scenario: str = typer.Option("bad_deploy"),
    url: str = typer.Option("http://localhost:8000"),
    token: str = typer.Option("dev-token", envvar="ALERTMANAGER_WEBHOOK_TOKEN"),
    timeout: int = typer.Option(120, help="seconds to wait for each phase"),
) -> None:
    """End-to-end demo: seed → fire → wait for brief → resolve → wait for postmortem."""
    t0 = time.monotonic()
    typer.echo(f"=== Vigil demo: {scenario} ===")
    seed()
    fire(scenario=scenario, url=url, token=token)

    def find_incident() -> dict | None:
        incidents = httpx.get(f"{url}/api/incidents", timeout=10.0).json()
        sc_alert = _load_scenario(scenario)["alert"]["labels"]
        mine = [i for i in incidents if i["service"] == sc_alert["service"]]
        active = [i for i in mine if i["status"] != "postmortem_done"]
        return (active or mine or [None])[0]

    typer.echo("waiting for brief…")
    incident = None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        incident = find_incident()
        if incident and incident.get("slack_message_ts"):
            break
        time.sleep(2)
    if not incident or not incident.get("slack_message_ts"):
        typer.echo("ERROR: brief did not post in time", err=True)
        raise typer.Exit(1)
    typer.echo(f"  brief posted for incident {incident['id']} (severity {incident['severity']})")

    resolve(scenario=scenario, url=url, token=token)
    typer.echo("waiting for postmortem…")
    deadline = time.monotonic() + timeout
    detail = None
    while time.monotonic() < deadline:
        detail = httpx.get(f"{url}/api/incidents/{incident['id']}", timeout=10.0).json()
        if detail.get("postmortem"):
            break
        time.sleep(2)
    if not detail or not detail.get("postmortem"):
        typer.echo("ERROR: postmortem did not generate in time", err=True)
        raise typer.Exit(1)

    elapsed = time.monotonic() - t0
    typer.echo(f"\n=== demo complete in {elapsed:.0f}s ===")
    typer.echo(f"incident:   {url}/api/incidents/{incident['id']}")
    typer.echo("postmortem preview:")
    typer.echo(detail["postmortem"]["markdown"][:600])


if __name__ == "__main__":
    app()
