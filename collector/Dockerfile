# FastAPI + Playwright collection service.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install Chromium and its OS-level dependencies first so the layer is cached.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/ms-playwright/ffmpeg-*

RUN groupadd --system collector \
    && useradd --system --gid collector --home /app collector \
    && mkdir -p /tmp/aq-jobs \
    && chown -R collector:collector /app /tmp/aq-jobs

COPY --chown=collector:collector collector /app/collector

USER collector
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2); sys.exit(0)"

CMD ["uvicorn", "collector.main:app", "--host", "0.0.0.0", "--port", "8000"]
