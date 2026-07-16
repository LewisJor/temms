#!/bin/bash
set -e

echo "Installing MLflow..."
pip install --no-cache-dir mlflow psycopg2-binary

echo "Starting MLflow server..."
# Serve artifacts through the tracking server (proxied access) instead of
# handing clients a raw --default-artifact-root path. With a local artifact
# root, remote clients (the host, and the TEMMS daemon container, which does
# not mount the artifact volume) cannot upload or download artifacts. Using
# --serve-artifacts + --artifacts-destination makes the default artifact root
# "mlflow-artifacts:/", so every client streams artifacts over HTTP and the
# Hub's package-from-mlflow path can fetch model files.
# MLflow 3's host-header (DNS-rebinding) protection only allowlists localhost
# and private IPs by default, so container-to-container calls that use the
# compose service hostname (e.g. the TEMMS daemon reaching
# http://mlflow-server:5000) are rejected with "Invalid Host header". Allow the
# internal service hostname plus localhost; override MLFLOW_SERVER_ALLOWED_HOSTS
# for other deployment topologies.
MLFLOW_ALLOWED_HOSTS="${MLFLOW_SERVER_ALLOWED_HOSTS:-mlflow-server*,localhost*,127.0.0.1*,[::1]*}"

mlflow server \
  --backend-store-uri "${MLFLOW_BACKEND_STORE_URI}" \
  --serve-artifacts \
  --artifacts-destination "${MLFLOW_ARTIFACT_ROOT}" \
  --allowed-hosts "${MLFLOW_ALLOWED_HOSTS}" \
  --host 0.0.0.0 \
  --port 5000
