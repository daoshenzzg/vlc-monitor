"""Video coordinates in Qt logical pixels; devicePixelRatio is intentionally absent."""

import math

Rect = tuple[float, float, float, float]


def content_rect(width: float, height: float, display_aspect: float) -> Rect:
    if width <= 0 or height <= 0 or not math.isfinite(display_aspect) or display_aspect <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    w = min(width, height * display_aspect)
    h = w / display_aspect
    return ((width - w) / 2, (height - h) / 2, w, h)


def to_normalized(point: tuple[float, float], content: Rect, *, clamp=False):
    x, y, w, h = content
    if w <= 0 or h <= 0:
        return None
    nx, ny = (point[0] - x) / w, (point[1] - y) / h
    if not clamp and not (0 <= nx <= 1 and 0 <= ny <= 1):
        return None
    return (min(1.0, max(0.0, nx)), min(1.0, max(0.0, ny)))


def to_display(rect: Rect, content: Rect) -> Rect:
    x, y, w, h = rect
    cx, cy, cw, ch = content
    return (cx + x * cw, cy + y * ch, w * cw, h * ch)


def normalized_from_points(start, end) -> Rect:
    x, y = min(start[0], end[0]), min(start[1], end[1])
    return (x, y, abs(end[0] - start[0]), abs(end[1] - start[1]))


def valid_size(rect: Rect, source_width: int, source_height: int, minimum=16) -> bool:
    return source_width > 0 and source_height > 0 and rect[2] * source_width + 1e-8 >= minimum and rect[3] * source_height + 1e-8 >= minimum


def move_rect(rect: Rect, dx: float, dy: float) -> Rect:
    x, y, w, h = rect
    return (min(max(0.0, x + dx), 1 - w), min(max(0.0, y + dy), 1 - h), w, h)


def resize_rect(rect: Rect, end, source_width: int, source_height: int, minimum=16) -> Rect:
    x, y, _, _ = rect
    mw, mh = minimum / max(1, source_width), minimum / max(1, source_height)
    return (x, y, min(1 - x, max(mw, end[0] - x)), min(1 - y, max(mh, end[1] - y)))


def pixel_bounds(rect: Rect, width: int, height: int) -> tuple[int, int, int, int]:
    """Return clamped half-open source bounds (x0, y0, x1, y1)."""
    if width <= 0 or height <= 0:
        raise ValueError("Source dimensions must be positive")
    x, y, w, h = rect
    if not all(math.isfinite(v) for v in rect) or w <= 0 or h <= 0:
        raise ValueError("Invalid region")
    x0, y0 = max(0, math.floor(x * width)), max(0, math.floor(y * height))
    x1, y1 = min(width, math.ceil((x + w) * width)), min(height, math.ceil((y + h) * height))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Region is outside the source")
    return x0, y0, x1, y1
