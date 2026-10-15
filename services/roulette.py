"""The 12x3 pitch grid shared by the roulette, Who Where When and the pricing tools.

Grid: 12 columns along the pitch x 3 rows across it, on absolute coordinates (home attacks to the
right, y = 100 is the top touchline). Cell number = column * 3 + row + 1, so 1-3 is the column at
the home team's own goal line and 34-36 the column at the goal it attacks; row 0 is the top.
"""
import math

COLS, ROWS, CELLS = 12, 3, 36
RED = frozenset({1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36})
CHIPS = (1, 5, 25, 100)  # quick-pick chips in the UI; any whole amount up to MAX_STAKE is accepted
MAX_STAKE = 1000


def _index_set(position: float, size: int) -> set[int]:
    """Indexes of the bands a coordinate falls in. A point exactly on a dividing line is in both."""
    scaled = min(max(position, 0.0), 100.0) / 100.0 * size
    low = min(int(math.floor(scaled)), size - 1)
    bands = {low}
    line = round(scaled)
    # On a line (within float error, from either side): the bands on both sides of it.
    if 0 < line < size and math.isclose(scaled, line, abs_tol=1e-9):
        bands |= {line - 1, line}
    return bands


def cells_at(x: float, y: float) -> set[int]:
    """Cells that contain the point. The team's line-tie rule (2026-09-24): every touching cell wins."""
    cols = _index_set(x, COLS)
    rows = _index_set(100.0 - y, ROWS)
    return {c * ROWS + r + 1 for c in cols for r in rows}


def mirror(cell: int) -> int:
    """The same zone seen from the other end: column and row both flip."""
    col, row = (cell - 1) // ROWS, (cell - 1) % ROWS
    return (COLS - 1 - col) * ROWS + (ROWS - 1 - row) + 1
