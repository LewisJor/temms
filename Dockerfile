FROM python:3.10-slim

# System deps for ONNX Runtime + OpenCV headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libglib2.0-0 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project metadata first (for better Docker layer caching)
COPY pyproject.toml .
COPY src/ src/

# Install TEMMS with simulation dependencies
RUN pip install --no-cache-dir -e ".[sim]"

# Copy example data, policies, and scripts
COPY examples/ /app/examples/
COPY deploy/temms.conf /etc/temms/temms.yaml
COPY scripts/ /app/scripts/

# Create TEMMS data directories
RUN mkdir -p /var/lib/temms/models \
    /var/lib/temms/cache \
    /var/lib/temms/packages \
    /etc/temms/policies

EXPOSE 8080

# Entrypoint: init -> generate models -> import -> start daemon
COPY scripts/docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
