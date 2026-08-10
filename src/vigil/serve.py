"""Dev server entrypoint. Exists because psycopg async cannot run on Windows'
default ProactorEventLoop, and uvicorn's CLI picks its own loop factory. We
run Server.serve() inside a loop we control. Production (Linux container)
uses the plain uvicorn CMD in the Dockerfile.

Note: no --reload; on Windows run `uv run vigil-serve` and restart on change.
"""

import asyncio
import selectors
import sys

import uvicorn


def main() -> None:
    config = uvicorn.Config("vigil.main:app", host="0.0.0.0", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        loop_factory = lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())  # noqa: E731
        with asyncio.Runner(loop_factory=loop_factory) as runner:
            runner.run(server.serve())
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
