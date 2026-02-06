#!/bin/bash
set -e

echo "Installing MLflow..."
pip install --no-cache-dir mlflow psycopg2-binary

echo "Starting MLflow server..."
mlflow server \
  --backend-store-uri "${MLFLOW_BACKEND_STORE_URI}" \
  --default-artifact-root "${MLFLOW_ARTIFACT_ROOT}" \
  --host 0.0.0.0 \
  --port 5000
