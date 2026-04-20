#!/usr/bin/env bash
# q-ai-tools-blink (standalone) — interactive bootstrap.
#
# Single-container setup: no Dex, no Caddy, no Docker compose.
# Just generates .env, deploys via arduino-app-cli, and sets up a tunnel.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="q-ai-tools-blink"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

command -v openssl >/dev/null 2>&1 || die "openssl is required."

HAS_TAILSCALE=0;   command -v tailscale   >/dev/null 2>&1 && HAS_TAILSCALE=1
HAS_CLOUDFLARED=0; command -v cloudflared >/dev/null 2>&1 && HAS_CLOUDFLARED=1
HAS_APPCLI=0;      command -v arduino-app-cli >/dev/null 2>&1 && HAS_APPCLI=1

# ---------------------------------------------------------------------------
# 1. Generate .env (skip if already exists)
# ---------------------------------------------------------------------------
ENV_FILE="$REPO_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    log "Found existing .env — reusing."
    # shellcheck disable=SC1090
    source "$ENV_FILE"
else
    echo
    echo "How will this MCP be reachable from the public internet?"
    echo "  1) Tailscale Funnel  (free, no domain needed)"
    echo "  2) Cloudflare Tunnel (needs a domain on Cloudflare)"
    read -rp "Selection [1]: " METHOD
    METHOD=${METHOD:-1}

    case "$METHOD" in
        1)
            [[ $HAS_TAILSCALE -eq 1 ]] || die "tailscale not installed."
            TS_DNS=$(tailscale status --json 2>/dev/null \
                | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["Self"]["DNSName"].rstrip("."))' \
                2>/dev/null || true)
            [[ -n "$TS_DNS" ]] || die "tailscale is not logged in."
            PUBLIC_URL="https://$TS_DNS"
            EXPOSURE="tailscale"
            ;;
        2)
            read -rp "Public hostname (e.g. mcp.example.com): " HOSTNAME
            [[ -n "$HOSTNAME" ]] || die "hostname required"
            PUBLIC_URL="https://$HOSTNAME"
            EXPOSURE="cloudflare"
            ;;
        *) die "invalid selection" ;;
    esac

    echo
    read -rp "Auth email: " AUTH_EMAIL
    [[ -n "$AUTH_EMAIL" ]] || die "email required"

    read -rsp "Auth password (min 8 chars): " AUTH_PASSWORD
    echo
    [[ ${#AUTH_PASSWORD} -ge 8 ]] || die "password must be at least 8 characters"

    log "Hashing password with bcrypt..."
    AUTH_PASSWORD_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw(b'$AUTH_PASSWORD', bcrypt.gensalt(12)).decode())")

    MCP_CLIENT_ID="mcp"
    MCP_CLIENT_SECRET="$(openssl rand -hex 32)"

    cat > "$ENV_FILE" <<EOF
EXPOSURE='$EXPOSURE'
PUBLIC_URL='$PUBLIC_URL'
STATIC_CLIENT_ID='$MCP_CLIENT_ID'
STATIC_CLIENT_SECRET='$MCP_CLIENT_SECRET'
AUTH_EMAIL='$AUTH_EMAIL'
AUTH_PASSWORD_HASH='$AUTH_PASSWORD_HASH'
CORS_ORIGINS=''
EOF
    chmod 600 "$ENV_FILE"
    log "Wrote $ENV_FILE"
fi

# ---------------------------------------------------------------------------
# 2. Public exposure
# ---------------------------------------------------------------------------
if [[ "${EXPOSURE:-}" == "tailscale" ]]; then
    log "Configuring Tailscale Funnel → http://localhost:7000"
    sudo tailscale serve  --bg --https=443 --set-path=/ http://localhost:7000 >/dev/null 2>&1 \
        || warn "tailscale serve may already be set"
    sudo tailscale funnel --bg --https=443 on >/dev/null 2>&1 \
        || warn "tailscale funnel may already be on"
elif [[ "${EXPOSURE:-}" == "cloudflare" ]]; then
    log "Cloudflare Tunnel — make sure your tunnel routes $PUBLIC_URL → http://localhost:7000"
fi

# ---------------------------------------------------------------------------
# 3. Deploy App Lab app
# ---------------------------------------------------------------------------
if [[ $HAS_APPCLI -eq 1 ]]; then
    APP_TARGET="${ARDUINOAPPS_DIR:-$HOME/ArduinoApps}/$APP_NAME"
    log "Syncing app to $APP_TARGET"
    mkdir -p "$APP_TARGET"
    cp -R "$REPO_DIR/." "$APP_TARGET/"
    # Remove setup artifacts from the deployed copy
    rm -f "$APP_TARGET/setup.sh" "$APP_TARGET/.env"
    # Write the app's .env (subset of root .env)
    cat > "$APP_TARGET/.env" <<EOF
PUBLIC_URL=$PUBLIC_URL
STATIC_CLIENT_ID=$STATIC_CLIENT_ID
STATIC_CLIENT_SECRET=$STATIC_CLIENT_SECRET
AUTH_EMAIL=$AUTH_EMAIL
AUTH_PASSWORD_HASH=$AUTH_PASSWORD_HASH
CORS_ORIGINS=${CORS_ORIGINS:-}
EOF
    chmod 600 "$APP_TARGET/.env"

    log "Starting App Lab app"
    arduino-app-cli app stop  "$APP_TARGET" >/dev/null 2>&1 || true
    arduino-app-cli app start "$APP_TARGET" \
        || warn "arduino-app-cli app start failed"
else
    warn "arduino-app-cli not found — deploy manually."
fi

cat <<EOF

✓ Setup complete.

  MCP endpoint: $PUBLIC_URL/blink
  Auth login:   $PUBLIC_URL/oauth/authorize

  Test:
    curl -sI $PUBLIC_URL/blink
    # expect 401 + WWW-Authenticate: Bearer ...

  Logs:
    arduino-app-cli app logs \$HOME/ArduinoApps/$APP_NAME --follow

EOF
