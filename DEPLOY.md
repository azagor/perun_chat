# PerunChat Deployment Notes

> For the full company-website + VPS context, see the sibling
> `perun-in-the-machine/company_site/DEPLOY.md`.

## Prerequisites

- A Linux host with Python 3.10+ and `systemd`.
- Nginx (or another reverse proxy) terminating TLS and proxying `/chat/` and
  `/v1/chat/ws` to the gateway.
- A 256-bit HMAC secret for signing HITL approval tokens.

## Install

```bash
# 1. Clone / checkout the code
sudo mkdir -p /opt/perun_chat
sudo chown $USER:$USER /opt/perun_chat
git clone https://github.com/azagor/perun_chat.git /opt/perun_chat
cd /opt/perun_chat

# 2. Create virtual environment and install
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 3. Create state directories
sudo mkdir -p /var/lib/perunchat
sudo useradd -r -s /bin/false perunchat || true
sudo chown -R perunchat:perunchat /var/lib/perunchat

# 4. Generate and set the HMAC secret and dashboard secret
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
DASHBOARD_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
{
    echo "PERUNCHAT_SECRET=$SECRET"
    echo "PERUNCHAT_DASHBOARD_SECRET=$DASHBOARD_SECRET"
} | sudo tee /etc/perunchat.env
# Restrict permissions
sudo chmod 600 /etc/perunchat.env
```

## systemd service

```bash
# The unit sources its secrets from /etc/perunchat.env (EnvironmentFile).
sudo cp systemd/perunchat.service /etc/systemd/system/perunchat.service

sudo systemctl daemon-reload
sudo systemctl enable --now perunchat
```

## Nginx

The company website Nginx config already contains the required `/chat/` and
`/v1/chat/ws` proxy blocks. After deploying this service, reload Nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## Verify

```bash
curl -s http://127.0.0.1:8765/health | python3 -m json.tool
sudo journalctl -u perunchat -f
```

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `PERUNCHAT_SECRET` | yes | — | 64-char hex HMAC key for approval tokens |
| `PERUNCHAT_DASHBOARD_SECRET` | yes* | — | Shared secret for the public agent dashboard. Required to enable key provisioning via the website. |
| `PERUNCHAT_DB_PATH` | no | `~/.perunchat/perunchat.db` | SQLite database path |
| `PERUNCHAT_HOST` | no | `127.0.0.1` | Gateway bind address |
| `PERUNCHAT_PORT` | no | `8765` | Gateway bind port |
| `PERUNCHAT_ENV` | no | — | Set to `test` for unit-test safe defaults |

*If `PERUNCHAT_DASHBOARD_SECRET` is missing in production, key-provisioning endpoints return 403.
