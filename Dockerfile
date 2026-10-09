FROM python:3.13-slim

# git: the device-builder keeps a version history of its config dir with it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ghbuilder ghbuilder

LABEL org.opencontainers.image.source="https://github.com/tonyupup/esphome-gh-builder"
LABEL org.opencontainers.image.description="ESPHome remote build server that compiles on GitHub Actions"

ENV PYTHONUNBUFFERED=1
# Identity (peer-link key, pairings) lives here; keep it on a persistent volume.
VOLUME /data
EXPOSE 6055
ENTRYPOINT ["python", "-m", "ghbuilder.server", "--remote-build-only", "--remote-build-port", "6055", "/data"]
