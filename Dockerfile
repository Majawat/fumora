FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FUMORA_CONFIG_DIR=/config \
    FUMORA_PORT=8792 \
    PUID=99 \
    PGID=100 \
    UMASK=0002

# bsdtar (libarchive) unpacks CBR/CB7/CBT for conversion to CBZ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libarchive-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fumora ./fumora
RUN pip install --no-cache-dir . && rm -rf /root/.cache

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8792
VOLUME ["/config"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"FUMORA_PORT\"]}/api/health', timeout=4)"

ENTRYPOINT ["/entrypoint.sh"]
