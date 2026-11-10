# Application image: the webhook server and the ARQ worker.
#
#   web:    uvicorn gita.web:app --host 0.0.0.0 --port 8000
#   worker: arq gita.worker.WorkerSettings
#
# The worker needs `git` (it syncs repository checkouts) and the Docker
# CLI (generated tests run in sandbox containers; see SECURITY.md for
# what mounting the Docker socket implies).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 gita
USER gita

EXPOSE 8000
CMD ["uvicorn", "gita.web:app", "--host", "0.0.0.0", "--port", "8000"]
