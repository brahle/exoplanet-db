# harbor.brahle.com/brahle/exoplanet-db — Exoplanet Database API & MCP server
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /src
COPY pyproject.toml README.md uv.lock /src/
COPY src /src/src
COPY alembic.ini /src/alembic.ini
COPY alembic /src/alembic

RUN uv venv /opt/venv --python 3.12 \
 && uv pip install --python /opt/venv/bin/python --no-cache .

FROM python:3.12-slim

RUN groupadd -r -g 10001 exoplanet && useradd -r -u 10001 -g exoplanet -d /app -s /usr/sbin/nologin exoplanet \
 && mkdir -p /app /tmp && chown exoplanet:exoplanet /app

COPY --from=build /opt/venv /opt/venv
COPY --from=build --chown=exoplanet:exoplanet /src/alembic.ini /app/alembic.ini
COPY --from=build --chown=exoplanet:exoplanet /src/alembic /app/alembic

ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 10001:10001

EXPOSE 8000 9090

ENTRYPOINT []
CMD ["uvicorn", "exoplanet_db.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "5"]
