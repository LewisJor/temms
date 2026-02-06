#!/bin/bash
set -e

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║       TEMMS Local MLflow Setup (Fully Offline)              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Detect OS
OS="$(uname -s)"
case "${OS}" in
    Linux*)     PLATFORM=Linux;;
    Darwin*)    PLATFORM=Mac;;
    *)          PLATFORM="UNKNOWN"
esac

echo "Platform detected: ${PLATFORM}"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 not found. Please install Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "Python version: ${PYTHON_VERSION}"

# Install MLflow
echo ""
echo "Installing MLflow..."
pip install mlflow psycopg2-binary

# Create directories
MLFLOW_DIR="${HOME}/mlflow-local"
echo ""
echo "Creating MLflow directory: ${MLFLOW_DIR}"
mkdir -p "${MLFLOW_DIR}"/{mlruns,artifacts,db}

# Setup choice
echo ""
echo "Choose MLflow setup:"
echo "  1) Simple (SQLite, single user)"
echo "  2) Production (PostgreSQL, multi-user) - requires Docker"
echo ""
read -p "Enter choice [1-2]: " SETUP_CHOICE

if [ "${SETUP_CHOICE}" = "1" ]; then
    echo ""
    echo "Setting up MLflow with SQLite backend..."

    # Create start script
    cat > "${MLFLOW_DIR}/start-mlflow.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Starting MLflow server (SQLite backend)..."
echo "Access UI at: http://localhost:5000"
echo ""

mlflow server \
  --backend-store-uri "sqlite:///${SCRIPT_DIR}/db/mlflow.db" \
  --default-artifact-root "${SCRIPT_DIR}/artifacts" \
  --host 127.0.0.1 \
  --port 5000
EOF

    chmod +x "${MLFLOW_DIR}/start-mlflow.sh"

    echo ""
    echo "✓ MLflow setup complete!"
    echo ""
    echo "To start MLflow:"
    echo "  ${MLFLOW_DIR}/start-mlflow.sh"
    echo ""
    echo "Access UI at: http://localhost:5000"

elif [ "${SETUP_CHOICE}" = "2" ]; then
    # Check Docker
    if ! command -v docker &> /dev/null; then
        echo "Error: Docker not found. Please install Docker first."
        exit 1
    fi

    echo ""
    echo "Setting up MLflow with PostgreSQL backend..."

    # Create docker-compose.yml
    cat > "${MLFLOW_DIR}/docker-compose.yml" << 'EOF'
version: '3.8'

services:
  postgres:
    image: postgres:14
    container_name: mlflow-postgres
    environment:
      POSTGRES_USER: mlflow
      POSTGRES_PASSWORD: mlflow
      POSTGRES_DB: mlflow
    volumes:
      - ./db:/var/lib/postgresql/data
    ports:
      - "5432:5432"
    restart: unless-stopped

  mlflow:
    image: python:3.10-slim
    container_name: mlflow-server
    depends_on:
      - postgres
    environment:
      MLFLOW_BACKEND_STORE_URI: postgresql://mlflow:mlflow@postgres:5432/mlflow
      MLFLOW_ARTIFACT_ROOT: /mlflow/artifacts
    volumes:
      - ./artifacts:/mlflow/artifacts
    ports:
      - "5000:5000"
    restart: unless-stopped
    command: >
      bash -c "pip install mlflow psycopg2-binary &&
               mlflow server
                 --backend-store-uri postgresql://mlflow:mlflow@postgres:5432/mlflow
                 --default-artifact-root /mlflow/artifacts
                 --host 0.0.0.0
                 --port 5000"
EOF

    # Create management scripts
    cat > "${MLFLOW_DIR}/start-mlflow.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "${SCRIPT_DIR}"

echo "Starting MLflow with PostgreSQL..."
docker-compose up -d

echo ""
echo "Waiting for services to start..."
sleep 5

echo "✓ MLflow is running!"
echo ""
echo "Access UI at: http://localhost:5000"
echo ""
echo "To view logs: docker-compose logs -f"
echo "To stop: ${SCRIPT_DIR}/stop-mlflow.sh"
EOF

    cat > "${MLFLOW_DIR}/stop-mlflow.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "${SCRIPT_DIR}"

echo "Stopping MLflow..."
docker-compose down

echo "✓ MLflow stopped"
EOF

    cat > "${MLFLOW_DIR}/backup-mlflow.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
BACKUP_DIR="${SCRIPT_DIR}/backups/$(date +%Y%m%d_%H%M%S)"

mkdir -p "${BACKUP_DIR}"

echo "Backing up MLflow..."

# Backup database
docker exec mlflow-postgres pg_dump -U mlflow mlflow > "${BACKUP_DIR}/mlflow.sql"

# Backup artifacts
cp -r "${SCRIPT_DIR}/artifacts" "${BACKUP_DIR}/"

echo "✓ Backup complete: ${BACKUP_DIR}"
EOF

    chmod +x "${MLFLOW_DIR}"/*.sh

    # Start services
    cd "${MLFLOW_DIR}"
    docker-compose up -d

    echo ""
    echo "✓ MLflow setup complete!"
    echo ""
    echo "Management commands:"
    echo "  Start:  ${MLFLOW_DIR}/start-mlflow.sh"
    echo "  Stop:   ${MLFLOW_DIR}/stop-mlflow.sh"
    echo "  Backup: ${MLFLOW_DIR}/backup-mlflow.sh"
    echo ""
    echo "Access UI at: http://localhost:5000"

else
    echo "Invalid choice. Exiting."
    exit 1
fi

# Create example training script
cat > "${MLFLOW_DIR}/example_train.py" << 'EOF'
"""
Example: Log a model to local MLflow
"""
import mlflow
import mlflow.sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.datasets import load_iris

# Point to local MLflow
mlflow.set_tracking_uri("http://127.0.0.1:5000")

# Create experiment
mlflow.set_experiment("temms-examples")

# Train simple model
X, y = load_iris(return_X_y=True)
model = LogisticRegression(max_iter=200)
model.fit(X, y)

# Log to MLflow
with mlflow.start_run(run_name="iris-classifier-v1"):
    mlflow.log_param("max_iter", 200)
    mlflow.log_metric("accuracy", model.score(X, y))
    mlflow.sklearn.log_model(
        model,
        "model",
        registered_model_name="iris-classifier"
    )

print("✓ Model logged to MLflow!")
print("View at: http://localhost:5000")
EOF

echo ""
echo "Example training script created:"
echo "  ${MLFLOW_DIR}/example_train.py"
echo ""
echo "To test MLflow:"
echo "  pip install scikit-learn"
echo "  python ${MLFLOW_DIR}/example_train.py"
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    Setup Complete!                           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
