# EC2 CI/CD Setup

This repository includes a GitHub Actions workflow that deploys to EC2 whenever code is pushed to `main`.

## 1. GitHub Secrets

Add these repository secrets in GitHub:

- `EC2_HOST`: Public IP or DNS of the EC2 instance
- `EC2_USER`: SSH user, for example `ubuntu`
- `EC2_SSH_PRIVATE_KEY`: Private key content for the EC2 instance
- `EC2_PORT`: SSH port, usually `22`
- `EC2_APP_DIR`: Absolute path of this repo on the EC2 instance
- `EC2_SYSTEMD_SERVICE`: systemd service name for the bot, for example `nifty-bot`

## 2. One-Time EC2 Setup

Clone the repository on the EC2 instance into the same path used in `EC2_APP_DIR`.

Create a systemd service file like `/etc/systemd/system/nifty-bot.service`:

```ini
[Unit]
Description=Nifty Algo Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/kite_bot
ExecStart=/home/ubuntu/kite_bot/.venv/bin/python -u index.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nifty-bot
sudo systemctl start nifty-bot
```

## 3. Runtime Files On EC2

Keep these files on the EC2 instance and do not depend on GitHub Actions to generate them:

- `config.txt`
- `access_token.txt`
- Telethon session files if required

If these are environment-specific, manage them directly on the server.

## 4. Deployment Flow

On every push to `main`, GitHub Actions will:

1. Check out the repo
2. Install Python dependencies
3. Run a syntax check
4. SSH to EC2
5. Reset the EC2 repo to `origin/main`
6. Rebuild the virtual environment dependencies
7. Restart the systemd service

## 5. Notes

- The workflow uses `git reset --hard origin/main` on EC2. Do not keep uncommitted changes in the server clone.
- If you want zero-downtime or rollback support later, switch to a release-directory deployment pattern.