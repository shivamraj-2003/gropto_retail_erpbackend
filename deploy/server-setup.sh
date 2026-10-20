#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu/Debian Hostinger VPS. Run it once as root:
#   curl -fsSL <this file> | bash     (or copy it up and run: sudo bash server-setup.sh)
# Optional: DEPLOY_PUBKEY="ssh-ed25519 AAAA... github-actions" bash server-setup.sh
#           adds that key so GitHub Actions can log in.
set -euo pipefail

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker

mkdir -p /opt/gropto/backups
chmod 755 /opt/gropto

if [ -n "${DEPLOY_PUBKEY:-}" ]; then
  install -m 700 -d /root/.ssh
  grep -qxF "$DEPLOY_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null || echo "$DEPLOY_PUBKEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi

# Firewall: SSH, HTTP, HTTPS only. The API itself is never exposed directly.
if command -v ufw >/dev/null; then
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw --force enable
fi

echo "Server ready. Next: add the GitHub secrets listed in DEPLOY.md and push to main."
