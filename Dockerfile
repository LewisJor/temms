FROM python:3.11-slim

# System deps for ONNX Runtime and archive handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libglib2.0-0 \
    curl \
    zstd \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system temms \
    && useradd --system --gid temms --home-dir /var/lib/temms --shell /usr/sbin/nologin temms

WORKDIR /app

# Copy project metadata first (for better Docker layer caching)
COPY pyproject.toml .
COPY src/ src/

# Production edge images install only the inference stack by default.
# Local simulation can opt into TEMMS_EXTRAS=sim at build time.
ARG TEMMS_EXTRAS=inference
RUN pip install --no-cache-dir -e ".[${TEMMS_EXTRAS}]"

# Copy example data, policies, and scripts
COPY examples/ /app/examples/
COPY deploy/temms.conf /etc/temms/temms.yaml
COPY scripts/ /app/scripts/

# Create TEMMS data directories
RUN mkdir -p /var/lib/temms/models \
    /var/lib/temms/cache \
    /var/lib/temms/packages \
    /var/lib/temms/benchmarks \
    /etc/temms/policies \
    && chown -R temms:temms /var/lib/temms /etc/temms /app/src /app/scripts /app/examples

EXPOSE 8080

# Entrypoint: init -> generate models -> import -> start daemon
COPY scripts/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER temms

ENTRYPOINT ["/entrypoint.sh"]
