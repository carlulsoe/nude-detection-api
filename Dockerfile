FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /service
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY tests ./tests
COPY examples ./examples
COPY deploy ./deploy
COPY LICENSE NOTICE README.md Dockerfile compose.yaml template.yaml .env.example .gitignore .dockerignore ./
RUN useradd --uid 10001 --create-home api && mkdir /service/data && chown api:api /service/data
USER api
EXPOSE 8000
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
