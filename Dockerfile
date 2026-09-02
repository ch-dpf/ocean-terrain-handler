FROM python:3.12-slim-bookworm

# g++/python headers: Cython CTB meshing+encode (C++17, no system zlib).
# libgomp1/libglib: opencv-python-headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libgomp1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt pyproject.toml setup.py ./
RUN pip3 install --no-cache-dir -r requirements.txt cython

COPY app ./app
RUN python3 setup.py build_ext --inplace \
    && rm -rf build

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
