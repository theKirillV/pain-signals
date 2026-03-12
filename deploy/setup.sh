#!/bin/bash
# Run this on your Hetzner VPS
# Usage: cd /var/www/pain-signals && sudo bash deploy/setup.sh

set -e

APP_DIR=/var/www/pain-signals

echo "==> Setting up Python venv"
cd $APP_DIR
python3 -m venv venv
venv/bin/pip install -r requirements.txt

echo "==> Setting permissions"
chown -R kirill:kirill $APP_DIR

echo "==> Installing systemd service"
cp $APP_DIR/deploy/pain-signals.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable pain-signals
systemctl start pain-signals

echo "==> Service status:"
systemctl status pain-signals --no-pager

echo ""
echo "==> DONE. Now add the nginx config:"
echo "    Copy the location block from deploy/pain-signals.nginx"
echo "    into your kirillv.com server block, then:"
echo "    sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "    Your app will be live at https://kirillv.com/pain-signals"
