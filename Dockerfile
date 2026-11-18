# Mini Booru image. Debian 12 base to match the production LXC.
FROM python:3.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MINIBOORU_DATA=/data
# ffmpeg = video thumbnails + sizes
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-mini.txt .
RUN pip install -r requirements-mini.txt
COPY minibooru ./minibooru
COPY healthcheck.py .

# the real user id is set at run time by docker-compose (PUID/PGID) so files on the bind mount belong to you
RUN mkdir /data && chmod 777 /data
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "/app/healthcheck.py"]
CMD ["python", "-m", "minibooru", "serve", "--host", "0.0.0.0", "--port", "8090"]
