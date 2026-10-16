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

if [ -f "${RUNNER_DIR}/.runner" ]; then
  echo ""
  echo "=== GitHub Actions runner already configured, skipping ==="
else
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
fi

echo ""
echo "=== Public ingress ==="
echo "TLS and the public vhost are served by the existing nginx-proxy + acme-companion"
echo "stack on this host. The engine container self-registers via VIRTUAL_HOST /"
echo "LETSENCRYPT_HOST env vars in docker-compose.yml; no extra ingress software is needed."
echo "Ensure the engine service is attached to the nginx-proxy Docker network (see"
echo "docker-compose.yml) so nginx-proxy can reach it."

echo ""
echo "=== Done ==="
echo ""
echo "Remaining steps:"
echo "  1. At your DNS provider, create an A record for your-domain.com pointing to"
echo "     this server's public IP."
echo "  2. Copy your .env to the server:"
echo "       cat .env | ssh root@<server-ip> \"cat > /opt/repo/.env\""
echo "  3. Merge to main after CI passes, then push a new v* release tag to deploy."
echo ""
echo "The dashboard is public and read-only. Mutating endpoints require ENGINE_API_TOKEN;"
echo "the Quidax webhook is locked to QUIDAX_WEBHOOK_ALLOWED_IPS via the X-Real-IP header"
echo "set by nginx-proxy."
