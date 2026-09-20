# Official Python base, pinned after an actual linux/amd64 pull.
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS runtime-deps

LABEL qfbench2.interface_version="2.0"
LABEL org.opencontainers.image.title="MicroCat QuantGuard"
LABEL org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/tmp XDG_CACHE_HOME=/tmp/.cache MPLCONFIGDIR=/tmp/matplotlib \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2

WORKDIR /opt/quantguard
COPY requirements-runtime.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-runtime.lock

FROM runtime-deps AS submission
ENV QUANTGUARD_ISOLATION=container
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && mkdir -p /app/output

# The official harness passes `solve --task-dir /input --out /app/output`.
# Do not set ENTRYPOINT: the required solve command is installed on PATH.
USER 65534:65534
CMD ["solve", "--help"]

# This target is local verification tooling and is not the submitted image.
FROM submission AS test-runner
USER 0:0
RUN pip install --no-cache-dir --no-compile pytest==9.1.1
USER 65534:65534

FROM submission AS release
