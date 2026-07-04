# Portfolio Tracker — Server

FastAPI backend + static website, fully containerised.

- **API**: `arthdeskapi.ashokitservices.com` → Docker uvicorn on port 8000
- **Website**: `arthdesk.ashokitservices.com` → Docker nginx on port 3000

---

## VM Setup (one-time)

### 1. Prerequisites

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Install Docker Compose plugin
sudo apt-get install -y docker-compose-plugin

# Verify
docker compose version
```

### 2. Stop host nginx if running

```bash
sudo systemctl stop nginx
sudo systemctl disable nginx
```

### 3. DNS

In your domain registrar add two A records pointing to the VM's public IP:

| Type | Name | Value |
|------|------|-------|
| A | `arthdeskapi` | `<VM IP>` |
| A | `arthdesk` | `<VM IP>` |

Get VM IP: `curl ifconfig.me`

### 4. Clone and configure

```bash
cd ~
git clone <repo-url> projects/server
cd projects/server

# Create required directories
mkdir -p data inbox errors logs scripts certbot/www certbot/conf
```

Copy `.env` values into `docker-compose.yml` environment section:

| Variable | Description |
|----------|-------------|
| `JWT_SECRET` | 32-byte random hex — `openssl rand -hex 32` |
| `ADMIN_USER` | Admin panel username |
| `ADMIN_PASS` | Admin panel password |
| `SMTP_PASS` | Gmail App Password (16 chars) — leave blank to disable emails |
| `BASE_URL` | `https://arthdeskapi.ashokitservices.com` |
| `AUTH_DB_PATH` | `/app/data/auth.db` |

### 5. Get SSL certificates (first time only)

nginx needs certs to start, but certbot needs nginx running to issue certs.
Bootstrap with a temporary HTTP-only config:

```bash
# Start nginx with only the port 80 block active
# Temporarily comment out the two `ssl` server blocks in nginx-proxy.conf
# leaving only the HTTP block, then:
docker compose up -d nginx certbot

# Issue cert for API subdomain
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d arthdeskapi.ashokitservices.com \
  --email sumitshark13@gmail.com --agree-tos --no-eff-email

# Issue cert for website subdomain
docker compose run --rm certbot certonly \
  --webroot -w /var/www/certbot \
  -d arthdesk.ashokitservices.com \
  --email sumitshark13@gmail.com --agree-tos --no-eff-email

# Restore full nginx-proxy.conf (uncomment the 443 blocks)
docker compose down
```

### 6. Start everything

```bash
docker compose up -d --build
```

Verify:
```bash
docker compose ps
curl https://arthdeskapi.ashokitservices.com/health
```

---

## Routine deploy

```bash
git pull && docker compose down && docker compose up -d --build
```

---

## Services

| Container | Image | Role |
|-----------|-------|------|
| `portfolio-server` | custom (Dockerfile) | FastAPI API |
| `portfolio-website` | nginx:alpine | Static website files |
| `portfolio-nginx` | nginx:alpine | Reverse proxy + SSL termination |
| `portfolio-certbot` | certbot/certbot | SSL cert auto-renewal (every 12h) |

---

## Volumes

| Path | Purpose |
|------|---------|
| `./data/` | SQLite databases (`portfolio_server.db`, `auth.db`) |
| `./inbox/` | Drop bhavcopy files here for manual registration |
| `./errors/` | Error logs |
| `./logs/` | App logs |
| `./certbot/conf/` | Let's Encrypt certs (persist across rebuilds) |
| `./certbot/www/` | ACME challenge webroot |

---

## Running tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

---

## Manual admin triggers

```bash
# Fetch today's EOD prices
curl -X POST https://arthdeskapi.ashokitservices.com/admin/fetch-prices

# Download bhavcopy
curl -X POST https://arthdeskapi.ashokitservices.com/admin/download-bhavcopy

# Sync bhavcopy to DB
curl -X POST https://arthdeskapi.ashokitservices.com/admin/sync-bhavcopy

# Populate instrument master
curl -X POST https://arthdeskapi.ashokitservices.com/admin/populate-instruments
```
