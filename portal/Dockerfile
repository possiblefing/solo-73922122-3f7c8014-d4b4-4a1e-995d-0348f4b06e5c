# Local mock of the legacy air-quality monitoring portal.
# Standard library only - keep the image deliberately small and old-school.
FROM python:3.12-slim

# Run as an unprivileged user.
RUN groupadd --system portal && useradd --system --gid portal --home /app portal
WORKDIR /app

COPY --chown=portal:portal app.py /app/app.py

USER portal
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2); sys.exit(0)"

CMD ["python", "-I", "/app/app.py"]
