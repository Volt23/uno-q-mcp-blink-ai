# q-ai-tools-blink

A single-container starter for running an **OAuth-gated remote MCP server** on an **Arduino UNO Q** — exposing the four onboard RGB LEDs and the 8×12 LED matrix as tools any OAuth 2.1 + PKCE-capable MCP client can call.

Client-agnostic by design: the code doesn't know which LLM or IDE is on the other end of the socket. Any client that speaks the standard MCP streamable-HTTP transport with OAuth 2.1 works.

```
 any MCP client
 (LLM chat UI,          ┌──────────────┐        ┌──────────────┐       ┌───────────────────┐
  IDE assistant,        │  MCP client  │ HTTPS  │   public     │       │   Arduino UNO Q   │
  inspector, ...) ─────▶│              │ ─────▶ │  Tailscale   │ ────▶ │                   │
                        │              │ OAuth  │  Funnel or   │       │  App Lab app:     │
                        │              │        │  Cloudflare  │       │   - FastAPI+FastMCP│
                        └──────────────┘        │  Tunnel      │       │   - OAuth 2.1 ────│── login form
                                                └──────────────┘       │   - msgpack RPC ──│──► STM32 sketch
                                                                       │                   │    LED3/4 + matrix
                                                                       └───────────────────┘
```

No Dex, no Caddy, no Docker compose. The app **is** the auth server — one container, one port, one process.

## What you get

**MCP tools:**

| Tool              | Description                                    |
| ----------------- | ---------------------------------------------- |
| `leds_read`       | Last-commanded state of LEDs 1–4               |
| `led_set`         | Set one LED (id 1–4, r/g/b)                    |
| `leds_all`        | Set all four LEDs to the same colour           |
| `leds_off`        | Turn all LEDs off                              |
| `matrix_draw`     | Render one 8×12 frame (values 0–7 per pixel)   |
| `matrix_clear`    | Clear the matrix                               |
| `matrix_marquee`  | Scroll arbitrary text across the matrix        |
| `matrix_stop`     | Stop any running marquee                       |
| `board_info`      | Static metadata about the board + endpoint     |

## Quickstart

Run on the UNO Q:

```bash
git clone https://github.com/Volt23/uno-q-mcp-blink-ai.git ~/q-ai-tools-blink
cd ~/q-ai-tools-blink
./setup.sh
```

`setup.sh` will:

1. Ask for **exposure method**: Tailscale Funnel (default) or Cloudflare Tunnel.
2. Ask for your **email + password** (hashed with bcrypt, stored in `.env`).
3. Generate an OAuth client secret.
4. Set up the public tunnel (Tailscale path; for Cloudflare, point your tunnel at `http://localhost:7000`).
5. Deploy the app via `arduino-app-cli`.
6. Print the MCP endpoint URL.

The first build takes 2–5 minutes (STM32 sketch compile). Subsequent boots are seconds.

## Connect an MCP client

Any MCP client that supports OAuth 2.1 with PKCE (S256) and RFC 7591 Dynamic Client Registration works:

1. Add a remote MCP server with URL: `https://<your-host>/blink`
2. The client auto-discovers endpoints, self-registers via DCR, and opens an auth-code flow.
3. You see a login form — enter the email + password from setup.
4. Done. Every subsequent request carries a signed JWT.

**Tested with:** Claude.ai, VS Code Copilot, Cursor, Zed, MCP Inspector.

## Auth flow in detail

The app itself implements the full OAuth 2.1 server (no external OIDC provider needed):

```
Client                         App (port 7000)
  │                                │
  ├─ GET /.well-known/oauth-*  ───▶│  discovery
  ├─ POST /oauth/register      ───▶│  DCR shim (returns static client)
  ├─ GET /oauth/authorize      ───▶│  login form
  │◀─── login page ────────────────│
  ├─ POST /oauth/authorize     ───▶│  validate credentials
  │◀─── 302 redirect + code ───────│
  ├─ POST /oauth/token         ───▶│  exchange code + PKCE for JWT
  │◀─── { access_token, ... } ─────│
  │                                │
  ├─ POST /blink  (Bearer JWT) ───▶│  MCP tools (verified)
  │◀─── tool results ──────────────│
```

JWTs are RS256-signed with a key generated at startup, `aud` set to the protected resource URL, 1-hour expiry. Refresh tokens are supported.

## Cloudflare Tunnel setup

If you chose Cloudflare in `setup.sh`:

```bash
# Create a tunnel (one-time)
cloudflared tunnel create uno-q-mcp

# Configure it
cat > ~/.cloudflared/config.yml <<EOF
tunnel: <tunnel-id>
credentials-file: ~/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: mcp.yourdomain.com
    service: http://localhost:7000
  - service: http_status:404
EOF

# Add DNS route
cloudflared tunnel route dns uno-q-mcp mcp.yourdomain.com

# Run it
cloudflared tunnel run uno-q-mcp
```

For persistence, install as a systemd service:
```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
```

## Tailscale Funnel setup

If you chose Tailscale (default), `setup.sh` handles it automatically:

```bash
sudo tailscale serve --bg --https=443 --set-path=/ http://localhost:7000
sudo tailscale funnel --bg --https=443 on
```

Your MCP endpoint becomes `https://<hostname>.<tailnet>.ts.net/blink`.

## Repo layout

```
uno-q-mcp-blink-ai/
├── README.md
├── setup.sh              # interactive bootstrap — run this first
├── app.yaml              # App Lab metadata
├── sketch/
│   ├── sketch.ino        # MCU: LED3/4 + matrix RPC providers
│   └── sketch.yaml       # sketch build profile + libraries
├── python/
│   ├── main.py           # FastAPI + FastMCP entry point
│   ├── auth.py           # OAuth 2.1 server + JWT middleware (all-in-one)
│   ├── hardware.py       # MCU msgpack RPC + MPU LED sysfs glue
│   ├── marquee.py        # text → 8×12 scrolling frame sequence
│   ├── font.py           # 3×6 bitmap font
│   └── requirements.txt
└── skill/
    └── mcp-tools.md      # tool usage guide for AI agents and MCP clients
```

## Troubleshooting

```bash
# Reachability
curl -sI https://<host>/blink
# expect: 401 Unauthorized, WWW-Authenticate: Bearer resource_metadata="..."

# Discovery documents
curl -s https://<host>/.well-known/oauth-protected-resource | jq
curl -s https://<host>/.well-known/oauth-authorization-server | jq

# Token exchange test (after login)
curl -s -X POST https://<host>/oauth/token \
  -d "grant_type=authorization_code&code=<code>&code_verifier=<verifier>&client_id=mcp&client_secret=<secret>"

# Logs
arduino-app-cli app logs ~/ArduinoApps/q-ai-tools-blink --follow
```

| Symptom | Cause | Fix |
|---------|-------|-----|
| `unauthorized` on every MCP call | Token expired or bad audience | Re-authenticate; check `PUBLIC_URL` in `.env` matches your actual hostname |
| Login form shows but 401 after submit | Wrong email or password | Check `AUTH_EMAIL` and `AUTH_PASSWORD_HASH` in `.env` |
| `PKCE verification failed` | Client sent wrong code_verifier | Client bug — retry the flow from scratch |
| 502 from tunnel | App not running | `arduino-app-cli app start ~/ArduinoApps/q-ai-tools-blink` |
| Stale registration (was working, now 401) | Client cached old DCR | Change `MCP_PATH` in `.env` (e.g. `/blink2`), restart app |

## Security model

The **host** is the root of trust. The stack defends against remote attackers, not someone with shell on the UNO Q.

- **Login** — bcrypt cost-12 password hash. No account enumeration (same error for wrong email or password).
- **MCP tokens** — RS256 JWTs, 1-hour expiry, issuer + audience verified per request.
- **PKCE** — S256 code challenge required for all authorization grants.
- **Secrets on disk** — `.env` created with mode `0600` by `setup.sh`.
- **No exposed admin** — credentials are set once during setup. To rotate, edit `.env` and restart the app.

## Architecture notes

**Why all-in-one auth?** MCP clients require OAuth 2.1 + PKCE + DCR. Delegating to a separate OIDC provider (Dex, Keycloak) introduces token-audience mismatches, Docker networking, extra containers, and a 10-minute Caddy build on ARM. For a single-user MCP on embedded hardware, ~260 lines of Python replaces all of that with zero external dependencies.

**Why not a static token?** Real MCP clients only speak OAuth 2.1 with PKCE + DCR. A raw bearer won't even register. The built-in OAuth server satisfies the standard flow without running a separate auth stack.

**Why the sketch?** LEDs 3, 4 and the matrix are MCU-attached (STM32). `arduino-app-cli` handles the sketch build + MCU flash + Router Bridge socket we talk msgpack-RPC to from Python. One command deploys the whole thing.

## License

MPL-2.0.
