# Pin the linux/amd64 manifest used by the Compose deployment.
FROM docker.m.daocloud.io/library/python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4 AS runtime

ARG TARGETPLATFORM
RUN test "$TARGETPLATFORM" = "linux/amd64" || { echo "unsupported target: $TARGETPLATFORM" >&2; exit 1; }

LABEL org.opencontainers.image.title="FastGPT Baidu OCR adapter" \
      org.opencontainers.image.description="Independent Baidu OCR to FastGPT file parsing adapter" \
      org.opencontainers.image.version="0.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp

# The image contains no build tools, database, persistent volume, or queue.
# Requirements are installed before application source so dependency layers
# remain cacheable while the final image still contains every runtime package.
WORKDIR /app
COPY service/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --requirement /app/requirements.txt \
    && python -m pip check

COPY service/app/ /app/app/

RUN test -f /app/app/main.py \
    && test -s /app/app/main.py \
    && test -z "$(find /app/app -type d -name '__pycache__' -print -quit)" \
    && test -z "$(find /app/app -type f \( -name '*.pyc' -o -name '*.pyo' \) -print -quit)" \
    && python -m compileall -q -f /app/app \
    && find /app/app -type d -name '__pycache__' -prune -exec rm -rf {} + \
    && test -z "$(find /app/app -type f \( -name '*.pyc' -o -name '*.pyo' \) -print -quit)"

RUN groupadd --gid 10001 ocradapter \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /tmp \
       --shell /usr/sbin/nologin ocradapter

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
