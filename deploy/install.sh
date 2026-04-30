#!/usr/bin/env bash
# Debian installer for headless-scraping-subsystem.
#
# Run as root. Idempotent — re-running upgrades the venv and reloads the
# unit. Default install path is /opt/headless-scraping-subsystem.

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/headless-scraping-subsystem}"
SERVICE_USER="${SERVICE_USER:-headless}"
ENV_FILE="${ENV_FILE:-/etc/headless-scraping-subsystem.env}"
UNIT_FILE="/etc/systemd/system/headless-scraping-subsystem.service"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

echo "==> Installing system packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git ca-certificates curl

# 1. service user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  echo "==> Creating service user '$SERVICE_USER'"
  useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin \
    "$SERVICE_USER"
fi

# 2. checkout
echo "==> Syncing repo to $INSTALL_DIR"
SRC_DIR="$(cd "$(dirname "$0")"/.. && pwd)"
mkdir -p "$INSTALL_DIR"
rsync -a --delete --exclude '.git' --exclude '.venv' \
      --exclude '__pycache__' "$SRC_DIR"/ "$INSTALL_DIR"/
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# 3. venv + pip
echo "==> Creating venv"
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/pip" install \
  -r "$INSTALL_DIR/requirements.txt"

# 4. Playwright browser + system deps for Chromium
echo "==> Installing Playwright Chromium"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/.venv/bin/playwright" install chromium

# `playwright install-deps chromium` is hardcoded for Ubuntu and breaks on
# Debian (e.g. Trixie has no `ttf-ubuntu-font-family` / `ttf-unifont`). We
# install the actually-needed runtime libraries directly. apt resolves the
# `t64` time_t-transition variants on Trixie automatically.
echo "==> Installing Chromium runtime libraries (Debian-safe list)"
apt-get install -y --no-install-recommends \
  libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libxkbcommon0 libpango-1.0-0 libasound2 libatspi2.0-0 \
  libnss3 libnspr4 libxshmfence1 \
  fonts-liberation fonts-noto-color-emoji fonts-unifont

# 5. env file
if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Creating $ENV_FILE from .env.example"
  install -m 0640 -o root -g "$SERVICE_USER" \
    "$INSTALL_DIR/.env.example" "$ENV_FILE"
  echo "    Edit $ENV_FILE and set BEARER_TOKEN before starting!"
else
  echo "==> $ENV_FILE already exists, leaving as-is"
fi

# 6. systemd unit
echo "==> Installing systemd unit"
install -m 0644 \
  "$INSTALL_DIR/deploy/systemd/headless-scraping-subsystem.service" \
  "$UNIT_FILE"

systemctl daemon-reload

echo
echo "==> Done. Next steps:"
echo "  1. \$EDITOR $ENV_FILE   # set BEARER_TOKEN"
echo "  2. systemctl enable --now headless-scraping-subsystem"
echo "  3. Splice deploy/nginx/files.entscheidsuche.ch.conf into your vhost"
echo "  4. systemctl reload nginx"
echo "  5. curl https://files.entscheidsuche.ch/headless/health"
