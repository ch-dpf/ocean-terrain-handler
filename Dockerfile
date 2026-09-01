FROM python:3.12-slim-bookworm

# docker.io talks to the host daemon via /var/run/docker.sock (CTB sidecar).
# libgomp1/libglib are needed by opencv-python-headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    libgomp1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
