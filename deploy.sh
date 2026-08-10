#!/bin/bash
# Deploy script — pulls from GitHub → deploys to star.panupong.net
set -e

cd /var/www/star
echo "[$(date)] Deploying..."

# Pull latest
git pull origin main

# Fix permissions
sudo chown -R www-data:www-data /var/www/star/*.html /var/www/star/*.css /var/www/star/*.js 2>/dev/null

# Reload nginx
sudo systemctl reload nginx

# Sync CDN from R2
bash /var/www/star/sync-cdn.sh 2>/dev/null || true

echo "[$(date)] Deploy complete ✓"
