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
echo "=== Done ==="
echo ""
echo "Remaining step — copy your .env to the server:"
echo "  scp .env root@100.102.148.33:/opt/repo/.env"
echo ""
echo "Then push to main to trigger the first deployment."
