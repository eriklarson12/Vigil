FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build
WORKDIR /app
COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev --no-install-project || uv sync --no-dev --no-install-project
COPY src/ src/
COPY simulator/ simulator/
RUN uv sync --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN useradd -m vigil
COPY --from=build /app/.venv /app/.venv
COPY src/ src/
COPY simulator/ simulator/
COPY services.yaml ./
COPY tests/fixtures/ tests/fixtures/
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER vigil
EXPOSE 8000
CMD ["uvicorn", "vigil.main:app", "--host", "0.0.0.0", "--port", "8000"]
