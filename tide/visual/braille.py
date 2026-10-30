"""Braille canvas: 2x4 dot grid per terminal cell (U+2800 .. U+28FF).

The visualizers draw into a dot grid and render one character per cell. Dot
bit layout follows the Unicode braille pattern:

=====  =====
1 4   bit0 bit3
2 5   bit1 bit4
3 6   bit2 bit5
7 8   bit6 bit7

so dot ``n`` (1-based) maps to bit ``(n - 1)`` with the column-major order
above: bits 0-2 are column 0 rows 0-2, bits 3-5 are column 1 rows 0-2, bit 6
is column 0 row 3 and bit 7 is column 1 row 3.
"""

from __future__ import annotations

BRAILLE_OFFSET = 0x2800
_EMPTY = chr(BRAILLE_OFFSET)

# dot -> bit mask
DOT_BITS = {
    (0, 0): 0b00000001,
    (0, 1): 0b00000010,
    (0, 2): 0b00000100,
    (0, 3): 0b01000000,
    (1, 0): 0b00001000,
    (1, 1): 0b00010000,
    (1, 2): 0b00100000,
    (1, 3): 0b10000000,
}


def encode_cell(bits: int) -> str:
    """Encode an 8-bit dot mask as one braille character."""
    return chr(BRAILLE_OFFSET + (bits & 0xFF))


def decode_cell(ch: str) -> int:
    """Inverse of :func:`encode_cell`; returns 0 for non-braille input."""
    code = ord(ch)
    if BRAILLE_OFFSET <= code <= BRAILLE_OFFSET + 0xFF:
        return code - BRAILLE_OFFSET
    return 0


class BrailleCanvas:
    """A ``width x height`` cell grid addressable as ``2*width x 4*height`` dots."""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(0, int(width))
        self.height = max(0, int(height))
        self._bits = bytearray(self.width * self.height)

    # -- dimensions ------------------------------------------------------
    @property
    def dot_width(self) -> int:
        return self.width * 2

    @property
    def dot_height(self) -> int:
        return self.height * 4

    def resize(self, width: int, height: int) -> None:
        self.width = max(0, int(width))
        self.height = max(0, int(height))
        self._bits = bytearray(self.width * self.height)

    def clear(self) -> None:
        self._bits = bytearray(self.width * self.height)

    # -- drawing ---------------------------------------------------------
    def set_dot(self, x: int, y: int, on: bool = True) -> None:
        """Set a dot in grid coordinates (``x`` in 0..2*width, ``y`` in 0..4*height)."""
        if x < 0 or y < 0 or x >= self.dot_width or y >= self.dot_height:
            return
        cell_x, local_x = divmod(x, 2)
        cell_y, local_y = divmod(y, 4)
        index = cell_y * self.width + cell_x
        mask = DOT_BITS[(local_x, local_y)]
        if on:
            self._bits[index] |= mask
        else:
            self._bits[index] &= ~mask & 0xFF

    def toggle_dot(self, x: int, y: int) -> None:
        if x < 0 or y < 0 or x >= self.dot_width or y >= self.dot_height:
            return
        cell_x, local_x = divmod(x, 2)
        cell_y, local_y = divmod(y, 4)
        index = cell_y * self.width + cell_x
        self._bits[index] ^= DOT_BITS[(local_x, local_y)]

    def plot(self, points, on: bool = True) -> None:
        """Plot many ``(x, y)`` dot pairs at once."""
        for x, y in points:
            self.set_dot(int(x), int(y), on)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int, on: bool = True) -> None:
        """Bresenham line in dot coordinates."""
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            self.set_dot(x0, y0, on)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def clear_cell(self, cx: int, cy: int) -> None:
        if 0 <= cx < self.width and 0 <= cy < self.height:
            self._bits[cy * self.width + cx] = 0

    # -- rendering -------------------------------------------------------
    def rows(self) -> list[str]:
        """Render each cell row as a string of braille characters."""
        out: list[str] = []
        for y in range(self.height):
            base = y * self.width
            row = "".join(
                encode_cell(self._bits[base + x]) for x in range(self.width)
            )
            out.append(row)
        return out

    def render(self) -> str:
        return "\n".join(self.rows())

    def cell_bits(self, cx: int, cy: int) -> int:
        if 0 <= cx < self.width and 0 <= cy < self.height:
            return self._bits[cy * self.width + cx]
        return 0
