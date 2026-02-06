#!/bin/bash
set -e

echo "Installing TEMMS..."

# Create system user
if ! id -u temms > /dev/null 2>&1; then
    sudo useradd -r -s /bin/false temms
fi

# Create directories
sudo mkdir -p /opt/temms
sudo mkdir -p /var/lib/temms/{models,cache,packages}
sudo mkdir -p /etc/temms/{policies,slots}
sudo mkdir -p /var/log/temms

# Set permissions
sudo chown -R temms:temms /var/lib/temms
sudo chown -R temms:temms /var/log/temms
sudo chmod 755 /etc/temms

# Install Python package
sudo python3 -m venv /opt/temms/venv
sudo /opt/temms/venv/bin/pip install --upgrade pip
sudo /opt/temms/venv/bin/pip install .

# Copy configuration
if [ ! -f /etc/temms/temms.yaml ]; then
    sudo cp deploy/temms.conf /etc/temms/temms.yaml
fi

# Install systemd service
sudo cp deploy/temms.service /etc/systemd/system/
sudo systemctl daemon-reload

echo "✓ TEMMS installed successfully"
echo ""
echo "Next steps:"
echo "  1. Edit configuration: /etc/temms/temms.yaml"
echo "  2. Initialize: temms init"
echo "  3. Import models: temms import /path/to/package/"
echo "  4. Enable service: sudo systemctl enable temms"
echo "  5. Start service: sudo systemctl start temms"
