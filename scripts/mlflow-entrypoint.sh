#!/bin/bash
set -e

echo "Installing MLflow..."
pip install --no-cache-dir mlflow psycopg2-binary

echo "Starting MLflow server..."
mlflow server \
  --backend-store-uri "${MLFLOW_BACKEND_STORE_URI}" \
  --default-artifact-root "${MLFLOW_ARTIFACT_ROOT}" \
  --host 0.0.0.0 \
  --allowed-hosts "${MLFLOW_ALLOWED_HOSTS:-localhost,127.0.0.1,mlflow-server,mlflow-server:5000}" \
  --port 5000
