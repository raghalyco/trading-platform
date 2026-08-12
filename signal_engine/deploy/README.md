# Deploying signal_engine to EC2 (t3.micro)

Goal: run signal_engine continuously for 1-2 weeks to accumulate a real
auto-trade journal, checkable from your phone each morning without SSH.

## Why a phone-login page at all

Kite access tokens expire **daily**. This app deliberately never automates
the actual Zerodha login (bot detection risk) — a human logs in for real,
every morning. `/admin/<token>/login` just moves the "paste the token back"
step from an SSH session onto a bookmarked URL you can open in your phone's
browser. You still do the real login in a real browser tab; this page only
receives the redirect URL/request_token afterward and swaps it into the
already-running process (no restart needed).

## 1. Launch / prepare the instance

- Instance: your existing **t3.micro** (1 vCPU burstable, 1GB RAM) — fine
  for this app; the setup script adds swap as a safety margin.
- AMI: Ubuntu 22.04 or 24.04 LTS.
- **Security group**: open port **8000** (or put nginx/a load balancer in
  front later) — but restrict the source to **your own IP** (or your
  phone's mobile carrier's IP range, which changes) rather than `0.0.0.0/0`.
  A dashboard showing your trading signals/journal shouldn't be open to the
  whole internet. If your IP changes often, use the AWS Console's "My IP"
  autofill each time you tighten the rule, or put a Cloudflare Tunnel /
  Tailscale in front instead of opening the port publicly at all.
- Also keep port 22 (SSH) restricted to your IP as usual.

## 2. Copy the repo and run setup

```bash
# from your local machine
scp -r /path/to/trading-platform ubuntu@<EC2_PUBLIC_IP>:~/trading-platform
ssh ubuntu@<EC2_PUBLIC_IP>
cd ~/trading-platform
bash signal_engine/deploy/setup_ec2.sh
```

This installs Python, adds 1GB swap, creates the venv, installs
`signal_engine/requirements.txt`, and registers a systemd service
(`signal-engine`) that restarts automatically if it crashes or the
instance reboots.

## 3. Fill in `.env` and start

```bash
nano ~/trading-platform/.env
```

```
KITE_API_KEY=your_key
KITE_API_SECRET=your_secret
SIGNAL_ENGINE_ADMIN_TOKEN=<generate one>
```

Generate the admin token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Then:

```bash
sudo systemctl start signal-engine
sudo systemctl status signal-engine   # should show "active (running)"
```

## 4. Daily routine (every morning before market open)

1. Open `http://<EC2_PUBLIC_IP>:8000/admin/<SIGNAL_ENGINE_ADMIN_TOKEN>/login`
   on your phone (bookmark it).
2. Tap **"Open Kite Login"** → log in with your real Zerodha
   user ID/password/TOTP in the browser tab that opens.
3. You'll land on a blank/error redirect page — that's expected. Copy the
   full URL from the address bar.
4. Go back to the login page tab, paste it into the box, tap **"Complete
   Login"**. You should see "✓ Logged in — live Kite data is now active."

That's it — no SSH needed for the daily step. The dashboard at
`http://<EC2_PUBLIC_IP>:8000/` will show `Source: LIVE (Kite)` once done,
and `auto_trade` will start capturing real ATM/OTM trades whenever a
signal hits `TAKE` @ ≥75% confidence, targeting the fixed +12 premium
points per trade.

**If you miss a day**: the app doesn't crash — `KiteFeed.from_shared_auth()`
raising just means the process is still running on whatever feed it had
(or simulator if it never got a token). It won't auto-capture real trades
until you complete that day's login, so any gap day is simply absent from
that day's data rather than corrupted.

## 5. Checking progress / pulling the report

Everything lands in `signal_engine/app/signal_engine/journal.db` (SQLite)
on the EC2 box. From your phone or laptop:

- **Live journal view**: `http://<EC2_PUBLIC_IP>:8000/` → "Journal" tab
- **Raw JSON**: `http://<EC2_PUBLIC_IP>:8000/api/journal`
- **CSV export**: `http://<EC2_PUBLIC_IP>:8000/api/journal/export.csv`
- **Daily P&L summary**: `http://<EC2_PUBLIC_IP>:8000/api/journal/daily-summary`

To pull the whole database file for offline analysis after the 1-2 week run:

```bash
scp ubuntu@<EC2_PUBLIC_IP>:~/trading-platform/signal_engine/app/signal_engine/journal.db ./
```

## 6. Logs / troubleshooting

```bash
sudo systemctl status signal-engine        # is it running?
sudo journalctl -u signal-engine -f        # live logs
sudo journalctl -u signal-engine --since today
```

## 7. Stopping / restarting manually

```bash
sudo systemctl restart signal-engine
sudo systemctl stop signal-engine
```
