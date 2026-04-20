# q-ai-tools-blink — MCP tool guide

Usage reference for AI agents and MCP clients controlling the Arduino UNO Q LEDs and matrix.

## LEDs (1–4)

| LED | Controller | Behavior |
|-----|------------|----------|
| 1   | MPU        | On/off per channel — any non-zero r/g/b value = fully on |
| 2   | MPU        | On/off per channel — same as LED 1 |
| 3   | MCU        | True 8-bit PWM — values 0–255 give real brightness control |
| 4   | MCU        | On/off per channel, active-low — same binary behavior as 1 & 2 |

**Tools:**

- `led_set(led, r, g, b)` — set one LED. Only LED 3 responds to intermediate values (e.g. `r=128`); LEDs 1, 2, 4 treat anything > 0 as fully on.
- `leds_all(r, g, b)` — set all four to the same color in one call.
- `leds_off()` — turn all four off.
- `leds_read()` — returns the last-commanded state (not a hardware readback).

### Tips

- Use LED 3 for color mixing or fade effects — it's the only one with real dimming.
- `leds_read` returns what you last sent, not what the hardware shows. If the MCU was reset independently, the cache is stale.

## Matrix (8×12, 3-bit grayscale)

An 8-row by 12-column LED grid. Each pixel accepts brightness 0–7.

**Tools:**

- `matrix_draw(frame)` — render one frame. `frame` is a list of 8 lists, each with 12 integers (0–7).
- `matrix_clear()` — set all pixels to 0.
- `matrix_marquee(text, frame_ms=80, brightness=5, loop=False)` — scroll text across the matrix.
- `matrix_stop()` — cancel a running marquee and clear the matrix.

### Critical: clear between operations

Always call `matrix_clear()` or `matrix_stop()` before switching between `matrix_draw` and `matrix_marquee`, or between different marquee texts. The previous frame/animation is not automatically cleared — you'll get visual artifacts or the old animation bleeding into the new one.

**Good:**
```
matrix_marquee("HELLO")     # wait for it to finish
matrix_clear()              # clean slate
matrix_draw([[...]])        # new static frame
```

**Bad:**
```
matrix_marquee("HELLO")
matrix_draw([[...]])        # artifacts from the marquee still running
```

### Marquee details

- The font is 3×6 pixels per character. Uppercase, lowercase, digits, and common punctuation are supported. Unknown characters render as a space.
- `frame_ms` controls scroll speed — lower = faster. Default 80ms is a comfortable reading pace.
- `brightness` is 1–7 (default 5).
- `loop=True` keeps scrolling indefinitely but **monopolizes the MCU bus** — all other LED and matrix tools will time out until you call `matrix_stop()`. Use one-shot (default) unless you specifically need a persistent display.
- A new `matrix_marquee` call automatically cancels any previous marquee before starting.

### Frame format

```
[
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 0 (top)
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 1
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 2
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 3
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 4
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 5
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 6
  [0,0,0,0,0,0,0,0,0,0,0,0],  # row 7 (bottom)
]
```

Each value: 0 = off, 7 = full brightness. Draw shapes, icons, or patterns by setting individual pixels.

## Introspection

- `board_info()` — returns board type, matrix dimensions, LED capabilities, MCP endpoint URL, issuer, and uptime.

## MCU bus constraints

All MCU operations (LED 3, LED 4, matrix) go through a single serialized RPC channel over a unix socket. Calls queue — they don't fail — but a long-running `matrix_marquee(loop=True)` blocks the queue until stopped. Plan accordingly:

1. Stop any looping marquee before sending other MCU commands.
2. Rapid-fire `matrix_draw` calls work fine for animation (the lock serializes them), but each frame has ~5ms of overhead from the RPC round-trip.
3. If a tool call times out, a previous looping marquee is the most likely cause — call `matrix_stop()` first.
