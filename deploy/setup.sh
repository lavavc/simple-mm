#!/usr/bin/env bash
# Run once on the VPS as root to set up the GitHub Actions runner and app directory.
set -euo pipefail

REPO="lavavc/automated-infra"

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
# Allow port 8000 only from the Tailscale interface; block it everywhere else
ufw allow in on tailscale0 to any port 8000
ufw deny 8000/tcp
ufw --force enable
echo "Firewall: port 8000 open on tailscale0 only."

echo ""
echo "=== Downloading GitHub Actions runner v${RUNNER_VERSION} ==="
mkdir -p /opt/actions-runner
cd /opt/actions-runner

curl -sSL -o runner.tar.gz \
  "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
tar xzf runner.tar.gz
rm runner.tar.gz

echo ""
echo "=== Next steps ==="
echo ""
echo "1. Get a runner registration token:"
echo "   https://github.com/${REPO}/settings/actions/runners/new"
echo ""
echo "2. Register the runner (paste your token):"
echo "   cd /opt/actions-runner"
echo "   ./config.sh --url https://github.com/${REPO} --token TOKEN --unattended --name vps-runner"
echo ""
echo "3. Install and start as a systemd service:"
echo "   cd /opt/actions-runner"
echo "   ./svc.sh install"
echo "   ./svc.sh start"
echo ""
echo "4. Copy your .env:"
echo "   scp .env root@77.42.32.180:/opt/cngn/.env"
echo ""
echo "Then push to main — the pipeline will build, test, and deploy automatically."
