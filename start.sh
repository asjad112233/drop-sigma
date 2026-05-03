#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLOUDFLARED="$HOME/.local/bin/cloudflared"
CF_LOG="/tmp/cf_tunnel.log"
ENV_FILE="$SCRIPT_DIR/.env"

cd "$SCRIPT_DIR"

echo "→ Stopping any running processes..."
pkill -f cloudflared 2>/dev/null || true
pkill -f "manage.py runserver" 2>/dev/null || true
sleep 1

echo "→ Starting Cloudflare tunnel..."
rm -f "$CF_LOG"
"$CLOUDFLARED" tunnel --url http://localhost:8000 --logfile "$CF_LOG" > /dev/null 2>&1 &
CF_PID=$!

# Wait up to 20 seconds for the tunnel URL to appear
TUNNEL_URL=""
for i in $(seq 1 20); do
    TUNNEL_URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$CF_LOG" 2>/dev/null | head -1)
    if [ -n "$TUNNEL_URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$TUNNEL_URL" ]; then
    echo "✗ Failed to get tunnel URL. Check $CF_LOG for errors."
    kill $CF_PID 2>/dev/null || true
    exit 1
fi

echo "→ Tunnel active: $TUNNEL_URL"

# Update WOOCOMMERCE_BASE_URL in .env (replace existing or append)
if grep -q "WOOCOMMERCE_BASE_URL" "$ENV_FILE"; then
    sed -i '' "s|WOOCOMMERCE_BASE_URL=.*|WOOCOMMERCE_BASE_URL=$TUNNEL_URL|" "$ENV_FILE"
else
    echo "WOOCOMMERCE_BASE_URL=$TUNNEL_URL" >> "$ENV_FILE"
fi

echo "→ .env updated with tunnel URL"
echo "→ Starting Django server..."
echo ""

"$SCRIPT_DIR/venv/bin/python" manage.py runserver
