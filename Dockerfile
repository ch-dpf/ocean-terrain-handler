FROM ghcr.io/osgeo/gdal:ubuntu-small-3.12.4

# docker.io client talks to host Docker via mounted /var/run/docker.sock.
# Do not install gdal-bin here; this image already ships GDAL 3.12.4.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-venv \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pyproject.toml .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
