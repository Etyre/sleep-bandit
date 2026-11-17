# Sleep Bandit on a private server (deploy/tailnet/README.md). The app is standard library only; the image adds
# time zone data (the 4 AM night rollover is in local time) and nothing else. Code is mounted, not copied.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s CMD curl -fsS -o /dev/null http://127.0.0.1:8777/ || exit 1
CMD ["python", "server.py"]
