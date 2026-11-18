FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt requirements-dev.txt /srv/
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY api /srv/api
COPY worker /srv/worker
COPY seed /srv/seed
COPY tests /srv/tests
COPY bench /srv/bench
COPY scripts /srv/scripts
COPY runner-images /srv/runner-images
COPY pytest.ini /srv/pytest.ini

RUN chmod +x /srv/scripts/*.sh && useradd --create-home --uid 10001 appuser

ARG COMMIT_SHA=unknown
ENV COMMIT_SHA=${COMMIT_SHA} \
    PYTHONPATH=/srv/api:/srv

WORKDIR /srv/api
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-2} --no-access-log"]
