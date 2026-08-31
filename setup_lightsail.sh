#!/usr/bin/env bash
# ==============================================================================
# MedCare AI Assistant — 1-Click AWS Lightsail Setup Script
# Run on fresh Ubuntu 22.04 / 24.04 instance
# ==============================================================================

set -e

echo "=================================================="
echo "  MedCare Assistant — AWS Lightsail Setup"
echo "=================================================="

# 1. Update system packages
echo "[1/5] Updating system packages..."
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y curl git ufw nginx certbot python3-certbot-nginx

# 2. Install Docker & Docker Compose
echo "[2/5] Installing Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    sudo systemctl enable docker
    sudo systemctl start docker
fi

# Install docker compose plugin if not present
sudo apt-get install -y docker-compose-plugin

# 3. Configure Firewall
echo "[3/5] Configuring firewall (ports 22, 80, 443)..."
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

# 4. Configure NGINX Reverse Proxy for SSE Streaming & WebSockets
echo "[4/5] Configuring NGINX reverse proxy..."
cat << 'EOF' | sudo tee /etc/nginx/sites-available/medcare
server {
    listen 80;
    server_name _;

    client_max_body_size 20M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;

        # SSE & Streaming configuration (critical for real-time LLM tokens)
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/medcare /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

# 5. Build and launch Docker container
echo "[5/5] Launching MedCare Docker Container..."
docker compose up -d --build

echo "=================================================="
echo "  ✅ MedCare Assistant is LIVE on your AWS Server!"
echo "  Access your server at: http://$(curl -s https://api.ipify.org)"
echo "=================================================="
