"""Hardware access for the UNO Q LEDs and LED matrix.

Two paths:
- LEDs 1 and 2 live on the MPU. We control them via arduino.app_utils.Leds.
- LEDs 3 and 4 plus the 8x12 matrix live on the MCU. We reach them over the
  Router's unix socket with msgpack-rpc.
"""

from __future__ import annotations

import socket
import threading
from typing import Any

import msgpack

ROUTER_SOCKET = "/var/run/arduino-router.sock"
MATRIX_ROWS = 8
MATRIX_COLS = 12
MATRIX_PIXELS = MATRIX_ROWS * MATRIX_COLS

# All MCU RPC traffic funnels through a single lock + a single persistent
# socket. The Router on the other end can't service more than one request
# at a time anyway, and opening a fresh unix socket per call wedges the
# Router after a handful of calls (sockets linger in its accept queue long
# enough that new connects block for the full RPC timeout). Serializing
# over one long-lived connection keeps the link steady and makes the
# timeout path deterministic.

_call_lock = threading.Lock()
_sock_lock = threading.Lock()
_sock: socket.socket | None = None

_msgid_lock = threading.Lock()
_msgid = 0


def _next_msgid() -> int:
    global _msgid
    with _msgid_lock:
        _msgid = (_msgid + 1) & 0x7FFFFFFF
        return _msgid


def _get_socket(timeout: float) -> socket.socket:
    """Return a connected socket, creating one if we don't already have a
    live connection. Caller holds `_sock_lock`."""
    global _sock
    if _sock is None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(ROUTER_SOCKET)
        _sock = s
    else:
        _sock.settimeout(timeout)
    return _sock


def _reset_socket() -> None:
    """Close the cached socket on error. Caller holds `_sock_lock`."""
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except Exception:
            pass
        _sock = None


def mcu_call(method: str, params: list[Any], timeout: float = 5.0) -> dict:
    """MessagePack-RPC request to the MCU sketch via the Router socket.

    Returns `{"ok": bool, "result": Any, "error": str | None}`. Serialised
    across all callers, reusing one long-lived unix socket; on any transport
    error the socket is closed and the next call reconnects.
    """
    request = [0, _next_msgid(), method, params]
    packed = msgpack.packb(request, use_bin_type=True)

    with _call_lock:
        for attempt in (1, 2):
            try:
                with _sock_lock:
                    client = _get_socket(timeout)
                    client.sendall(packed)

                    buffer = b""
                    for _ in range(8):
                        chunk = client.recv(1024)
                        if not chunk:
                            _reset_socket()
                            break
                        buffer += chunk
                        try:
                            unpacked = msgpack.unpackb(
                                buffer, max_array_len=256, max_map_len=256, raw=False
                            )
                            if isinstance(unpacked, list) and len(unpacked) >= 4:
                                error = unpacked[2]
                                result = unpacked[3]
                                return {
                                    "ok": error is None,
                                    "result": result,
                                    "error": error,
                                }
                        except msgpack.exceptions.ExtraData:
                            break
                        except Exception:
                            continue
                    # Fell out of the loop without a parseable reply —
                    # discard the socket so the next attempt starts clean.
                    _reset_socket()
                if attempt == 1:
                    continue
                return {"ok": False, "result": None, "error": "incomplete response"}
            except (socket.timeout, OSError) as exc:
                with _sock_lock:
                    _reset_socket()
                if attempt == 1:
                    continue
                return {"ok": False, "result": None, "error": f"timed out ({exc})"}
    return {"ok": False, "result": None, "error": "unreachable"}


# --- MPU LED helpers (LEDs 1 and 2) ------------------------------------------
#
# arduino.app_utils.Leds exposes set_led1_color(r, g, b) and set_led2_color(r, g, b),
# each taking three booleans (LEDs 1 and 2 are on/off per channel, no PWM).
#
# We import lazily so unit tests that don't have the arduino runtime can still
# import this module.

def _mpu_set(led: int, r: int, g: int, b: int) -> None:
    from arduino.app_utils import Leds  # type: ignore

    on_r, on_g, on_b = bool(r), bool(g), bool(b)
    if led == 1:
        Leds.set_led1_color(on_r, on_g, on_b)
    elif led == 2:
        Leds.set_led2_color(on_r, on_g, on_b)
    else:
        raise ValueError(f"LED {led} is not MPU-controlled")


# --- Unified LED state -------------------------------------------------------
#
# The hardware has no "read this LED's current color" API, so we cache what we
# last sent. Good enough for a demo; for higher fidelity you'd add a provider on
# the MCU that reports back.

_state: dict[int, dict[str, int]] = {
    1: {"r": 0, "g": 0, "b": 0},
    2: {"r": 0, "g": 0, "b": 0},
    3: {"r": 0, "g": 0, "b": 0},
    4: {"r": 0, "g": 0, "b": 0},
}


def _clamp8(v: int) -> int:
    return max(0, min(255, int(v)))


def leds_read() -> dict[int, dict[str, int]]:
    return {led: dict(state) for led, state in _state.items()}


def led_set(led: int, r: int, g: int, b: int) -> dict:
    if led not in (1, 2, 3, 4):
        raise ValueError(f"LED {led} does not exist; valid ids are 1-4")
    r, g, b = _clamp8(r), _clamp8(g), _clamp8(b)

    if led in (1, 2):
        _mpu_set(led, r, g, b)
    elif led == 3:
        res = mcu_call("set_led3_color", [r, g, b])
        if not res["ok"]:
            raise RuntimeError(f"MCU error: {res['error']}")
    elif led == 4:
        res = mcu_call("set_led4_color", [bool(r), bool(g), bool(b)])
        if not res["ok"]:
            raise RuntimeError(f"MCU error: {res['error']}")

    _state[led] = {"r": r, "g": g, "b": b}
    return dict(_state[led])


def leds_all(r: int, g: int, b: int) -> dict[int, dict[str, int]]:
    for led in (1, 2, 3, 4):
        led_set(led, r, g, b)
    return leds_read()


def leds_off() -> dict[int, dict[str, int]]:
    return leds_all(0, 0, 0)


# --- Matrix -----------------------------------------------------------------

def _validate_frame(frame: list[list[int]]) -> bytes:
    """Accept an 8x12 grid of brightness values 0..7 and pack to 104 bytes.

    The Arduino_LED_Matrix library uses an 8×13 internal canvas, so each
    12-pixel row gets a padding byte appended."""
    if len(frame) != MATRIX_ROWS:
        raise ValueError(f"frame must have {MATRIX_ROWS} rows, got {len(frame)}")
    out = bytearray(MATRIX_ROWS * 13)
    i = 0
    for row in frame:
        if len(row) != MATRIX_COLS:
            raise ValueError(f"each row must have {MATRIX_COLS} cols")
        for v in row:
            vi = int(v)
            if vi < 0 or vi > 7:
                raise ValueError("pixel values must be 0..7")
            out[i] = vi
            i += 1
        i += 1  # 13th column padding
    return bytes(out)


def matrix_draw(frame: list[list[int]]) -> None:
    # Bridge's std::vector<uint8_t> provider deserialises from msgpack's
    # `bin` type — pass the raw bytes here, NOT `list(data)`. Sending an
    # array of ints instead hangs (and eventually crashes) the sketch.
    data = _validate_frame(frame)
    res = mcu_call("matrix_draw", [data])
    if not res["ok"]:
        raise RuntimeError(f"MCU error: {res['error']}")


def matrix_clear() -> None:
    matrix_draw([[0] * MATRIX_COLS for _ in range(MATRIX_ROWS)])
