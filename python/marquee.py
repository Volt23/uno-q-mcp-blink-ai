"""Scrolling text marquee for the 8x12 UNO Q LED matrix.

Renders a string into a sequence of 8x12 frames suitable for matrix_draw().
The 6-row font strip (uppercase body + descender row) sits between a 1-row top
margin and a 1-row bottom margin; the whole thing scrolls right-to-left.
"""

from __future__ import annotations

from font import FONT_COLS, FONT_ROWS, glyph
from hardware import MATRIX_COLS, MATRIX_ROWS

CHAR_GAP = 1
ROW_TOP_MARGIN = 1   # blank rows above the glyph strip (8 - 6 - 1 = 1)
ROW_BOTTOM_MARGIN = 1


def _render_strip(text: str) -> list[list[int]]:
    """Render the text into an 8-row by N-column on/off strip.

    Rows are MATRIX_ROWS tall; columns are dense — one per glyph column.
    """
    assert ROW_TOP_MARGIN + FONT_ROWS + ROW_BOTTOM_MARGIN == MATRIX_ROWS

    char_cells = [glyph(c) for c in text]
    total_cols = sum(FONT_COLS + CHAR_GAP for _ in char_cells)
    if total_cols == 0:
        return [[0] * 0 for _ in range(MATRIX_ROWS)]

    strip = [[0] * total_cols for _ in range(MATRIX_ROWS)]
    x = 0
    for cell in char_cells:
        for row_idx, bitmask in enumerate(cell):
            y = ROW_TOP_MARGIN + row_idx
            for col in range(FONT_COLS):
                bit = (bitmask >> (FONT_COLS - 1 - col)) & 1
                strip[y][x + col] = bit
        x += FONT_COLS + CHAR_GAP
    return strip


def frames_for(
    text: str,
    brightness: int = 5,
    loop: bool = True,
) -> list[list[list[int]]]:
    """Produce the full list of 8x12 frames for scrolling `text` once.

    If `loop` is True, the strip is padded on the right with a MATRIX_COLS-wide
    gap so the tail exits cleanly before the head re-enters on the next cycle.
    """
    brightness = max(1, min(7, int(brightness)))

    strip = _render_strip(text)
    strip_cols = len(strip[0]) if strip else 0
    if strip_cols == 0:
        return []

    # Left pad so the first frame shows the text just arriving on the right edge.
    left_pad = MATRIX_COLS
    # Right pad so the text fully exits to the left before the loop restart.
    right_pad = MATRIX_COLS if loop else MATRIX_COLS
    padded_cols = left_pad + strip_cols + right_pad
    padded = [[0] * padded_cols for _ in range(MATRIX_ROWS)]
    for row in range(MATRIX_ROWS):
        for c in range(strip_cols):
            padded[row][left_pad + c] = strip[row][c]

    frames: list[list[list[int]]] = []
    total_shifts = padded_cols - MATRIX_COLS + 1
    for shift in range(total_shifts):
        frame = [
            [padded[r][shift + c] * brightness for c in range(MATRIX_COLS)]
            for r in range(MATRIX_ROWS)
        ]
        frames.append(frame)
    return frames
