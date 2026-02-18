#!/usr/bin/env bash
# Run once on the VPS as root to set up the GitHub Actions runner and app directory.
set -euo pipefail

REPO="lavavc/automated-infra"
RUNNER_USER="github-runner"
RUNNER_DIR="/opt/actions-runner"

# Check https://github.com/actions/runner/releases for the latest version
RUNNER_VERSION="2.321.0"

echo "=== Setting up /opt/repo ==="
if [ ! -d /opt/repo ]; then
  git clone "https://github.com/${REPO}.git" /opt/repo
fi
mkdir -p /opt/repo/data
chown -R "$RUNNER_USER:$RUNNER_USER" /opt/repo
echo "/opt/repo is owned by $RUNNER_USER."

echo ""
echo "=== Creating runner user ==="
useradd -m -s /bin/bash "$RUNNER_USER" || echo "User $RUNNER_USER already exists, skipping."
usermod -aG docker "$RUNNER_USER"
echo "User $RUNNER_USER added to docker group."

echo ""
echo "=== Downloading GitHub Actions runner v${RUNNER_VERSION} ==="
mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"

curl -sSL -o runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
tar xzf runner.tar.gz
rm runner.tar.gz
chown -R "$RUNNER_USER:$RUNNER_USER" "$RUNNER_DIR"

echo ""
echo "=== Runner is ready to configure ==="
echo ""
echo "1. Get your registration token at:"
echo "   https://github.com/${REPO}/settings/actions/runners/new"
echo ""
read -rp "Paste the token here: " RUNNER_TOKEN

echo ""
echo "=== Registering runner ==="
su - "$RUNNER_USER" -c "
  cd $RUNNER_DIR
  ./config.sh \
    --url https://github.com/${REPO} \
    --token ${RUNNER_TOKEN} \
    --unattended \
    --name vps-runner
"

echo ""
echo "=== Installing systemd service ==="
cd "$RUNNER_DIR"
./svc.sh install "$RUNNER_USER"
./svc.sh start
echo "Runner service started."

echo ""
echo "=== Installing cloudflared ==="
if ! command -v cloudflared &>/dev/null; then
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb"
  dpkg -i /tmp/cloudflared.deb
  rm /tmp/cloudflared.deb
else
  echo "cloudflared already installed, skipping."
fi

echo ""
echo "=== Cloudflare Tunnel setup ==="
echo ""
read -rp "Enter the dashboard hostname (e.g. engine.yourdomain.com): " CF_HOSTNAME

echo ""
echo "--- Step 1: Authenticate with Cloudflare ---"
echo "A URL will appear below. Open it in your browser and log in."
cloudflared tunnel login

echo ""
echo "--- Step 2: Creating tunnel 'cngn' ---"
if cloudflared tunnel list 2>/dev/null | grep -q "cngn"; then
  echo "Tunnel 'cngn' already exists, skipping creation."
else
  cloudflared tunnel create cngn
fi

TUNNEL_ID=$(cloudflared tunnel list --output json 2>/dev/null \
  | python3 -c "import json,sys; t=json.load(sys.stdin); print(next(x['id'] for x in t if x['name']=='cngn'))")
CREDS_FILE="/root/.cloudflared/${TUNNEL_ID}.json"

mkdir -p /etc/cloudflared
cat > /etc/cloudflared/config.yml <<EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${CREDS_FILE}

ingress:
  - hostname: ${CF_HOSTNAME}
    service: http://localhost:8000
  - service: http_status:404
EOF

echo ""
echo "--- Step 3: Creating DNS record ---"
cloudflared tunnel route dns cngn "${CF_HOSTNAME}"

echo ""
echo "--- Step 4: Installing and starting cloudflared service ---"
if systemctl is-active --quiet cloudflared; then
  systemctl restart cloudflared
else
  cloudflared service install
  systemctl enable cloudflared
  systemctl start cloudflared
fi
echo "Cloudflare tunnel running → https://${CF_HOSTNAME}"

echo ""
echo "=== Done ==="
echo ""
echo "Remaining steps:"
echo "  1. Copy your .env to the server:"
echo "       cat .env | ssh root@<server-ip> \"cat > /opt/repo/.env\""
echo "  2. In the Cloudflare Zero Trust dashboard, create an Access application"
echo "     for https://${CF_HOSTNAME} and set your allowed email policy."
echo "  3. Push to main to trigger the first deployment."
