FROM python:3.12-slim AS base

WORKDIR /app

COPY pyproject.toml .
COPY tunarr_autoscheduler/ tunarr_autoscheduler/

FROM base AS test

COPY tests/ tests/
RUN pip install --no-cache-dir ".[dev]"

CMD ["python", "-m", "pytest", "tests/"]

FROM base AS runtime

RUN pip install --no-cache-dir .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /root/.tunarr

EXPOSE 8000

CMD ["/entrypoint.sh"]
