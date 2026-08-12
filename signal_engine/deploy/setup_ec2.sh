#!/usr/bin/env bash
# One-time EC2 t3.micro setup for signal_engine, with the phone-login page.
# Tested against Ubuntu 22.04/24.04 AMIs. Run as the ubuntu user (not root).
#
# Usage (from your local machine, after the instance is up):
#   scp -r trading-platform ubuntu@<EC2_PUBLIC_IP>:~/trading-platform
#   ssh ubuntu@<EC2_PUBLIC_IP>
#   cd ~/trading-platform && bash signal_engine/deploy/setup_ec2.sh
set -euo pipefail

REPO_ROOT="$HOME/trading-platform"
cd "$REPO_ROOT"

echo "== 1. System packages =="
sudo apt-get update -y
sudo apt-get install -y python3.11 python3.11-venv python3-pip

echo "== 2. t3.micro has only 1GB RAM - add a 1GB swap file so pandas/numpy =="
echo "==    scans don't OOM-kill the process during a burst. =="
if [ ! -f /swapfile ]; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "== 3. Python venv + deps (signal_engine only) =="
cd signal_engine
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
cd "$REPO_ROOT"

echo "== 4. .env — fill in your Kite API key/secret + admin login token =="
if [ ! -f .env ]; then
  cat > .env <<'EOF'
KITE_API_KEY=
KITE_API_SECRET=
# Long random secret for the phone login page URL - generate with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(24))"
SIGNAL_ENGINE_ADMIN_TOKEN=
EOF
  echo "Created .env — EDIT IT NOW before starting the service:"
  echo "  nano $REPO_ROOT/.env"
else
  echo ".env already exists, leaving it as-is."
fi

echo "== 5. systemd service =="
sudo cp signal_engine/deploy/signal-engine.service /etc/systemd/system/signal-engine.service
sudo systemctl daemon-reload
sudo systemctl enable signal-engine

echo
echo "Setup done. Next steps:"
echo "  1. Edit $REPO_ROOT/.env with your real KITE_API_KEY / KITE_API_SECRET"
echo "     and a random SIGNAL_ENGINE_ADMIN_TOKEN."
echo "  2. sudo systemctl start signal-engine"
echo "  3. sudo systemctl status signal-engine"
echo "  4. Open http://<EC2_PUBLIC_IP>:8000/admin/<SIGNAL_ENGINE_ADMIN_TOKEN>/login"
echo "     on your phone once to complete today's Kite login."
echo "  5. Bookmark that URL — this is your daily morning routine from now on."
