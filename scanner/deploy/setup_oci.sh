#!/usr/bin/env bash
# One-time Oracle Cloud (OCI) VM setup for scanner/ (Kite Scanner Bot).
# Written for the Always Free Ampere A1 (ARM) shape running Ubuntu 22.04+,
# but works on any Ubuntu LTS release with a default python3. Run as the
# ubuntu user (not root) - OCI's Ubuntu images use "ubuntu" as the default
# SSH user, same as AWS.
#
# Usage (after the instance is up and you can SSH in):
#   ssh ubuntu@<OCI_PUBLIC_IP>
#   git clone https://github.com/raghalyco/trading-platform.git
#   cd trading-platform && bash scanner/deploy/setup_oci.sh
set -euo pipefail

REPO_ROOT="$HOME/trading-platform"
cd "$REPO_ROOT"

echo "== 1. System packages =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip nginx

echo "== 2. Swap file - cheap insurance against an OOM-kill during a big scan =="
if [ ! -f /swapfile ]; then
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "== 3. OCI Ubuntu images ship with iptables rules that block inbound"
echo "==    traffic EVEN AFTER you open the port in the OCI console's"
echo "==    Security List / NSG - this trips up almost everyone the first"
echo "==    time. Explicitly allow the ports this app needs: =="
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || sudo iptables-save | sudo tee /etc/iptables/rules.v4 >/dev/null || true

echo "== 4. Python venv + deps (scanner only) =="
cd scanner
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate
cd "$REPO_ROOT"

echo "== 5. .env — fill in your real Kite + Telegram values =="
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — EDIT IT NOW before starting the service:"
  echo "  nano $REPO_ROOT/.env"
else
  echo ".env already exists, leaving it as-is."
fi

echo "== 6. systemd service =="
sudo cp scanner/deploy/kite-scanner.service /etc/systemd/system/kite-scanner.service
sudo systemctl daemon-reload
sudo systemctl enable kite-scanner

echo "== 7. nginx reverse proxy (port 80 -> gunicorn on 127.0.0.1:8000) =="
sudo cp scanner/deploy/nginx_scanner.conf /etc/nginx/sites-available/kite-scanner
sudo ln -sf /etc/nginx/sites-available/kite-scanner /etc/nginx/sites-enabled/kite-scanner
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

echo
echo "Setup done. Next steps:"
echo "  1. Edit $REPO_ROOT/.env with your real KITE_API_KEY / KITE_API_SECRET"
echo "     (and TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID if you want alerts here too)."
echo "  2. One-time-per-trading-day Kite login (manual, needs your own browser):"
echo "       cd $REPO_ROOT && scanner/.venv/bin/python -m shared.kite_auth"
echo "     -> open the printed login URL yourself, log in, paste the request_token"
echo "        (or full redirect URL) back into this SSH session."
echo "  3. sudo systemctl start kite-scanner"
echo "  4. sudo systemctl status kite-scanner"
echo "  5. Open http://<OCI_PUBLIC_IP>/ in your browser."
echo
echo "Kite tokens expire nightly (~midnight IST) - step 2 is a daily routine,"
echo "not a one-time setup. Consider a shell alias or a note in your calendar."
