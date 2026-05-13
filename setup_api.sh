#!/bin/bash
# Arya Premium Mini App API - Permanent VPS Setup Script
# Run: bash setup_api.sh

set -e

echo "=== Arya Premium API - Permanent Setup ==="

# 1. Install Nginx
echo "[1/4] Installing Nginx..."
sudo apt update -qq && sudo apt install nginx -y -qq

# 2. Configure Nginx reverse proxy
echo "[2/4] Configuring Nginx..."
sudo tee /etc/nginx/sites-available/aryaapi > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;

        # CORS headers
        add_header 'Access-Control-Allow-Origin' '*' always;
        add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS' always;
        add_header 'Access-Control-Allow-Headers' 'Content-Type, Authorization' always;

        if ($request_method = OPTIONS) {
            return 204;
        }
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/aryaapi /etc/nginx/sites-enabled/aryaapi
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx && sudo systemctl enable nginx
echo "    ✓ Nginx configured and running on port 80"

# 3. Install Python dependencies
echo "[3/4] Installing Python dependencies..."
pip3 install -q fastapi uvicorn motor python-multipart

# 4. Create systemd service for API
echo "[4/4] Creating systemd service..."
WORKDIR=$(pwd)
PYTHON_PATH=$(which python3)

sudo tee /etc/systemd/system/aryaapi.service > /dev/null <<EOF
[Unit]
Description=Arya Premium Mini App API
After=network.target

[Service]
User=$(whoami)
WorkingDirectory=${WORKDIR}
ExecStart=${PYTHON_PATH} ${WORKDIR}/mini_app_api.py
Restart=always
RestartSec=5
Environment=BOT_USERNAME=UseAryaBot

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable aryaapi
sudo systemctl restart aryaapi
sleep 2
sudo systemctl status aryaapi --no-pager

echo ""
echo "======================================"
echo "✅ SETUP COMPLETE!"
echo "======================================"
echo ""
echo "API is now permanently running at:"
echo "  http://$(curl -s ifconfig.me):80"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status aryaapi   # status check"
echo "  sudo systemctl restart aryaapi  # restart"
echo "  sudo journalctl -u aryaapi -f   # live logs"
echo "======================================"
