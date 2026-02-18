#!/usr/bin/env bash
# Run once on the VPS as root to set up the GitHub Actions runner and app directory.
set -euo pipefail

REPO="lavavc/automated-infra"
RUNNER_USER="github-runner"
RUNNER_DIR="/opt/actions-runner"

# Check https://github.com/actions/runner/releases for the latest version
RUNNER_VERSION="2.321.0"

echo "=== Creating app directory ==="
mkdir -p /opt/cngn/data
echo "Place your production .env at /opt/cngn/.env before starting the container."

echo ""
echo "=== Placing docker-compose.yml ==="
cp "$(dirname "$0")/../docker-compose.yml" /opt/cngn/docker-compose.yml

echo ""
echo "=== Configuring firewall ==="
ufw allow in on tailscale0 to any port 8000
ufw deny 8000/tcp
ufw --force enable
echo "Firewall: port 8000 open on tailscale0 only."

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
echo "=== Done ==="
echo ""
echo "Remaining step — copy your .env to the server:"
echo "  scp .env root@77.42.32.180:/opt/cngn/.env"
echo ""
echo "Then push to main to trigger the first deployment."
