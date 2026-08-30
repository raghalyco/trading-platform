# Deploying the scanner to Oracle Cloud (OCI)

Runs the scanner dashboard on an OCI Always Free Ampere A1 (ARM) instance,
behind nginx, managed by systemd so it survives reboots and crashes.

## 1. Create the instance (OCI Console — you do this part)

- **Instance** → Create Instance
- **Image**: Ubuntu 22.04 (or newer LTS)
- **Shape**: Ampere A1 Flex — Always Free tier gives you up to 4 OCPU /
  24 GB total across your Always Free A1 instances; 2 OCPU / 12 GB is
  plenty for this app
- Add your SSH public key (or let OCI generate a key pair for you and
  download the private key)
- Leave networking on the default VCN unless you already have one

## 2. Open the ports (OCI Console — you do this part)

Your instance's subnet **Security List** (or a Network Security Group, if
you attached one) needs inbound rules for:

- TCP 22 (SSH) — ideally restricted to your own IP, not `0.0.0.0/0`
- TCP 80 (HTTP) — see the access-control note in `nginx_scanner.conf`
  before opening this to everyone
- TCP 443 (HTTPS) — once you add a domain + TLS cert (not covered by
  `setup_oci.sh`; ask if you want this wired up once you have a domain)

This is a separate layer from the VM's own firewall — see the next step.

## 3. Run the setup script (on the VM, over SSH)

```bash
ssh ubuntu@<OCI_PUBLIC_IP>
git clone https://github.com/raghalyco/trading-platform.git
cd trading-platform
bash scanner/deploy/setup_oci.sh
```

This installs Python/nginx, adds a swap file, opens 80/443 in the VM's
own `iptables` (Oracle's Ubuntu images block inbound traffic here even
after you open the OCI Security List — this trips up almost everyone the
first time), creates the venv, installs `scanner/requirements.txt`, and
registers `kite-scanner.service` + the nginx reverse proxy.

## 4. Fill in `.env` and log in to Kite (on the VM)

```bash
nano ~/trading-platform/.env       # KITE_API_KEY / KITE_API_SECRET / Telegram
cd ~/trading-platform
scanner/.venv/bin/python -m shared.kite_auth
```

`shared/kite_auth.py` is a manual, interactive login (open a URL yourself,
paste back a token) — it works fine over SSH, no domain or public callback
needed. Kite access tokens expire nightly around midnight IST, so this is
a daily-morning step, not one-time.

## 5. Start it

```bash
sudo systemctl start kite-scanner
sudo systemctl status kite-scanner
journalctl -u kite-scanner -f      # tail logs
```

Visit `http://<OCI_PUBLIC_IP>/`.

## Why gunicorn runs with exactly 1 worker

`app.py` keeps its scan caches and the two background Telegram-alert
threads (`_trending_alert_loop`, `_episodic_pivot_alert_loop`) as
in-process state, not in Redis or a database. A second gunicorn worker
would be a second, independent process with its *own* copy of that
state — duplicate Telegram alerts (each worker's loop fires
independently on the same fresh breakout) and requests randomly served
from whichever worker's cache happens to be stale. `--threads 4` gives
you request concurrency without that problem. See the comment in
`kite-scanner.service` — don't "optimize" this to more workers later
without re-architecting the cache/alert layer first.

## Redeploying after a `git pull`

```bash
cd ~/trading-platform && git pull
scanner/.venv/bin/pip install -r scanner/requirements.txt
sudo systemctl restart kite-scanner
```
