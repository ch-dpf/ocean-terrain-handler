FROM python:3.12-slim-bookworm AS wheel-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

COPY pyproject.toml setup.py MANIFEST.in README.md LICENSE.md THIRD_PARTY_NOTICES.md ./
COPY app ./app
COPY benchmarks ./benchmarks
COPY scripts ./scripts

# Build a wheel for the image's target CPU. Build dependencies remain in this stage.
RUN python3 -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY --from=wheel-builder /wheels/*.whl /tmp/wheels/
RUN pip3 install --no-cache-dir --no-deps /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && python3 -m app.services.ctb.native_check

ENV PYTHONUNBUFFERED=1
