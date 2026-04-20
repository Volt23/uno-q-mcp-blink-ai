"""q-ai-tools-blink — MCP server entry point (standalone edition).

Serves an OAuth-gated MCP over streamable-HTTP, exposing the Arduino UNO Q's
four onboard RGB LEDs and 8x12 LED matrix as tools any MCP client can call.

All-in-one: OAuth 2.1 server + MCP endpoint + hardware bridge in a single
container. No external auth provider, no reverse proxy, no extra services.
Just expose port 7000 via a public tunnel (Tailscale Funnel or Cloudflare).
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv("/app/.env")

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import auth
import hardware
from marquee import frames_for


# FastMCP auto-enables DNS-rebinding protection whenever the `host` setting
# looks like localhost, and it only whitelists `127.0.0.1:*` / `localhost:*`
# in that mode. We're fronted by Caddy + a public tunnel, so every request's
# Host header is the public hostname — without an explicit allow list here,
# every proxied request is rejected with 421. Pull the hostname from
# PUBLIC_URL and allow it (plus a few friendlies for local testing).
_public_host = auth.PUBLIC_URL.split("://", 1)[-1].split("/", 1)[0]

mcp = FastMCP(
    name="q-ai-tools-blink",
    instructions=(
        "Control the four onboard RGB LEDs and the 8x12 LED matrix of an "
        "Arduino UNO Q. LEDs 1 and 2 are on/off per channel (MPU-controlled); "
        "LED 3 is full 8-bit PWM; LED 4 is on/off per channel (MCU-controlled). "
        "The matrix accepts brightness 0..7 per pixel on an 8-row, 12-column grid."
    ),
    # FastMCP's default is "/mcp", which would make the full endpoint
    # <MCP_PATH>/mcp after mounting. Setting this to "/" means the mount
    # prefix itself is the endpoint, which is what MCP clients expect.
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[_public_host, "127.0.0.1:*", "localhost:*"],
        allowed_origins=[auth.PUBLIC_URL, "http://127.0.0.1:*", "http://localhost:*"],
    ),
)


# --- LED tools ---------------------------------------------------------------

@mcp.tool()
def leds_read() -> dict:
    """Return the last-commanded state of LEDs 1-4 as `{id: {r, g, b}}`."""
    return {str(k): v for k, v in hardware.leds_read().items()}


@mcp.tool()
def led_set(led: int, r: int = 0, g: int = 0, b: int = 0) -> dict:
    """Set one LED. `led` is 1-4; `r`/`g`/`b` are 0-255 (LEDs 1, 2, 4 treat
    any non-zero channel as on — only LED 3 is true PWM)."""
    return hardware.led_set(led, r, g, b)


@mcp.tool()
def leds_all(r: int = 0, g: int = 0, b: int = 0) -> dict:
    """Set all four LEDs to the same colour in one call."""
    return {str(k): v for k, v in hardware.leds_all(r, g, b).items()}


@mcp.tool()
def leds_off() -> dict:
    """Turn all four LEDs off."""
    return {str(k): v for k, v in hardware.leds_off().items()}


# --- Matrix tools ------------------------------------------------------------

@mcp.tool()
def matrix_draw(frame: list[list[int]]) -> dict:
    """Render one frame on the matrix. `frame` is 8 rows of 12 values 0..7."""
    hardware.matrix_draw(frame)
    return {"ok": True, "rows": hardware.MATRIX_ROWS, "cols": hardware.MATRIX_COLS}


@mcp.tool()
def matrix_clear() -> dict:
    """Clear the matrix (all pixels off)."""
    hardware.matrix_clear()
    return {"ok": True}


# Marquee animation is managed as a single asyncio task. A fresh
# `matrix_marquee` call cancels any previous one so overlapping invocations
# don't flicker the display.

_marquee_task: asyncio.Task | None = None


async def _run_marquee(
    frames: list[list[list[int]]], frame_ms: int, loop: bool
) -> None:
    try:
        while True:
            for frame in frames:
                hardware.matrix_draw(frame)
                await asyncio.sleep(frame_ms / 1000.0)
            if not loop:
                break
    except asyncio.CancelledError:
        hardware.matrix_clear()
        raise


async def _cancel_marquee() -> None:
    global _marquee_task
    if _marquee_task and not _marquee_task.done():
        _marquee_task.cancel()
        try:
            await _marquee_task
        except asyncio.CancelledError:
            pass
    _marquee_task = None


@mcp.tool()
async def matrix_marquee(
    text: str,
    frame_ms: int = 80,
    brightness: int = 5,
    loop: bool = False,
) -> dict:
    """Scroll `text` across the matrix once. Cancels any previous marquee first.

    `frame_ms` sets the per-column shift interval (default 80ms).
    `brightness` is 1..7 (default 5).
    `loop=True` keeps scrolling until `matrix_stop()` is called — but while
    a loop is running it monopolises the MCU bus, so other LED / matrix
    tools will time out until the loop is stopped. Default is one-shot for
    that reason.
    """
    global _marquee_task
    await _cancel_marquee()

    frames = frames_for(text, brightness=brightness, loop=loop)
    if not frames:
        return {"ok": True, "frames": 0}

    _marquee_task = asyncio.create_task(_run_marquee(frames, frame_ms, loop))
    return {
        "ok": True,
        "frames": len(frames),
        "duration_ms": len(frames) * frame_ms,
    }


@mcp.tool()
async def matrix_stop() -> dict:
    """Stop any running marquee and clear the matrix."""
    await _cancel_marquee()
    hardware.matrix_clear()
    return {"ok": True}


# --- Introspection -----------------------------------------------------------

_start_time = time.time()


@mcp.tool()
def board_info() -> dict:
    """Static metadata about the board and MCP endpoint."""
    return {
        "board": "Arduino UNO Q",
        "matrix": {
            "rows": hardware.MATRIX_ROWS,
            "cols": hardware.MATRIX_COLS,
            "bits": 3,
        },
        "leds": {
            "1": {"controller": "MPU", "channels": "r/g/b on-off"},
            "2": {"controller": "MPU", "channels": "r/g/b on-off"},
            "3": {"controller": "MCU", "channels": "r/g/b 8-bit PWM"},
            "4": {"controller": "MCU", "channels": "r/g/b on-off (active low)"},
        },
        "mcp_endpoint": f"{auth.PROTECTED_RESOURCE}",
        "issuer": auth.ISSUER,
        "uptime_s": int(time.time() - _start_time),
    }


# --- App wiring --------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Clear the display on both startup and shutdown, and run FastMCP's
    session manager so the streamable-HTTP app can accept requests (newer
    FastMCP versions require the session manager's task group to be open
    for the lifetime of the ASGI app)."""
    for side_effect in (hardware.matrix_clear, hardware.leds_off):
        try:
            side_effect()
        except Exception:
            pass
    async with mcp.session_manager.run():
        try:
            yield
        finally:
            for side_effect in (hardware.matrix_clear, hardware.leds_off):
                try:
                    side_effect()
                except Exception:
                    pass


app = FastAPI(title="q-ai-tools-blink", lifespan=lifespan, openapi_url=None)

auth.install(app)


@app.get("/", response_class=HTMLResponse)
async def status_page() -> str:
    return f"""<!doctype html>
<meta charset="utf-8">
<title>q-ai-tools-blink</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; color: #222 }}
  code {{ background: #f4f4f5; padding: .1rem .3rem; border-radius: .25rem }}
  h1 {{ font-weight: 600 }}
  .ok {{ color: #15803d }}
  .muted {{ color: #6b7280 }}
</style>
<h1>q-ai-tools-blink — <span class="ok">running</span></h1>
<p>OAuth-gated MCP server for the Arduino UNO Q onboard LEDs and matrix.
Paste the endpoint below into any MCP client that supports OAuth 2.1 + PKCE.</p>
<p><strong>MCP endpoint:</strong> <code>{auth.PROTECTED_RESOURCE}</code></p>
<p class="muted">
  Issuer: <code>{auth.ISSUER}</code><br>
  Protected resource metadata: <a href="/.well-known/oauth-protected-resource">/.well-known/oauth-protected-resource</a><br>
  Authorization server metadata: <a href="/.well-known/oauth-authorization-server">/.well-known/oauth-authorization-server</a>
</p>
"""


# The streamable-HTTP mount MUST come after all other route registrations on
# this prefix, otherwise it will catch sub-paths that the auth router or any
# future admin UI wants to own.
app.mount(auth.MCP_PATH, mcp.streamable_http_app())


# Some MCP clients (VS Code's Copilot, for example) POST to the bare mount
# prefix `/blink` without a trailing slash. Starlette's default response is a
# 307 to `/blink/`, but since uvicorn sits behind a reverse proxy it builds
# the Location with `http://` and Node's fetch refuses to follow a scheme
# downgrade — the connection just fails. Rewrite the path in-place so the
# mounted ASGI app sees the request as if the trailing slash were there.
@app.middleware("http")
async def _normalise_mcp_prefix(request, call_next):
    if request.scope["path"] == auth.MCP_PATH:
        request.scope["path"] = auth.MCP_PATH + "/"
        request.scope["raw_path"] = (auth.MCP_PATH + "/").encode()
    return await call_next(request)


def main() -> None:
    port = int(os.environ.get("PORT", "7000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
