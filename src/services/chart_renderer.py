"""chart_renderer — simple, dependency-light chart PNGs drawn with Pillow.

Rendered IN-HOUSE (no third-party chart service, no data leaves our infra) and
with no native/binary deps beyond Pillow (a pure manylinux wheel — Lambda-safe).
Charts feed the Telegram "visual dashboard" (Stage-3 extension / N2). Numbers
come from the shared accounting engine, so the pictures match the reports.

Each function writes a PNG to /tmp and returns its path (or None on failure —
callers must handle None gracefully and fall back to the text card).
"""

import logging
import os

logger = logging.getLogger(__name__)

# Brand palette
_GREEN = (39, 174, 96)
_RED = (192, 57, 43)
_INK = (44, 62, 80)
_GREY = (149, 165, 166)
_LIGHT = (236, 240, 241)
_WHITE = (255, 255, 255)

_W, _H = 900, 500          # canvas
_PAD_L, _PAD_R = 70, 30
_PAD_T, _PAD_B = 70, 90


def _font(size):
    """Load a TrueType font if available, else Pillow's default bitmap font."""
    from PIL import ImageFont
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _naira_short(v):
    """Compact money label for axes: 1.2M / 45k / 900."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "0"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        return f"{sign}{a/1_000_000:.1f}M".replace(".0M", "M")
    if a >= 1_000:
        return f"{sign}{a/1_000:.0f}k"
    return f"{sign}{int(a)}"


def _text_w(draw, text, font):
    try:
        b = draw.textbbox((0, 0), text, font=font)
        return b[2] - b[0]
    except Exception:
        return len(text) * 7


def _blank(title):
    """Empty-state canvas with just the title + a 'no data' line."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (_W, _H), _WHITE)
    d = ImageDraw.Draw(img)
    d.text((_PAD_L, 28), title, fill=_INK, font=_font(30))
    d.text((_PAD_L, _H // 2 - 10), "No data for this period yet.",
           fill=_GREY, font=_font(22))
    return img, d


def _save(img, name):
    try:
        path = f"/tmp/{name}"
        img.save(path, "PNG")
        return path
    except Exception as e:
        logger.error(f"chart save failed: {e}")
        return None


def bar_chart(title, labels, values, filename="chart_bar.png", positive_color=_GREEN):
    """Vertical bar chart. `labels`/`values` are parallel lists."""
    from PIL import ImageDraw  # noqa
    try:
        pairs = [(str(l), float(v)) for l, v in zip(labels or [], values or [])]
        if not pairs:
            img, _ = _blank(title)
            return _save(img, filename)

        from PIL import Image
        img = Image.new("RGB", (_W, _H), _WHITE)
        d = ImageDraw.Draw(img)
        d.text((_PAD_L, 24), title, fill=_INK, font=_font(30))

        plot_l, plot_r = _PAD_L, _W - _PAD_R
        plot_t, plot_b = _PAD_T, _H - _PAD_B
        plot_w, plot_h = plot_r - plot_l, plot_b - plot_t

        vmax = max((v for _, v in pairs), default=0) or 1
        # axis baseline
        d.line([(plot_l, plot_b), (plot_r, plot_b)], fill=_INK, width=2)

        n = len(pairs)
        slot = plot_w / n
        bar_w = min(slot * 0.6, 110)
        af = _font(18)
        vf = _font(18)
        for i, (lbl, val) in enumerate(pairs):
            cx = plot_l + slot * i + slot / 2
            h = int(plot_h * (max(val, 0) / vmax))
            x0, x1 = cx - bar_w / 2, cx + bar_w / 2
            color = positive_color if val >= 0 else _RED
            d.rectangle([x0, plot_b - h, x1, plot_b], fill=color)
            # value on top
            vt = _naira_short(val)
            d.text((cx - _text_w(d, vt, vf) / 2, plot_b - h - 24), vt, fill=_INK, font=vf)
            # label under axis (truncated)
            lt = lbl if len(lbl) <= 12 else lbl[:11] + "…"
            d.text((cx - _text_w(d, lt, af) / 2, plot_b + 10), lt, fill=_GREY, font=af)
        return _save(img, filename)
    except Exception as e:
        logger.error(f"bar_chart failed: {e}")
        return None


def trend_chart(title, points, filename="chart_trend.png"):
    """Line chart. `points` = [(label, value), ...] in chronological order.
    Negative values render below a zero line (losses)."""
    from PIL import ImageDraw
    try:
        pts = [(str(l), float(v)) for l, v in (points or [])]
        if len(pts) < 2:
            img, _ = _blank(title)
            return _save(img, filename)

        from PIL import Image
        img = Image.new("RGB", (_W, _H), _WHITE)
        d = ImageDraw.Draw(img)
        d.text((_PAD_L, 24), title, fill=_INK, font=_font(30))

        plot_l, plot_r = _PAD_L, _W - _PAD_R
        plot_t, plot_b = _PAD_T, _H - _PAD_B
        plot_w, plot_h = plot_r - plot_l, plot_b - plot_t

        vals = [v for _, v in pts]
        vmax, vmin = max(vals), min(vals)
        if vmax == vmin:
            vmax += 1
        span = vmax - vmin

        def y(v):
            return plot_t + plot_h * (1 - (v - vmin) / span)

        # zero line if the range straddles 0
        if vmin < 0 < vmax:
            zy = y(0)
            d.line([(plot_l, zy), (plot_r, zy)], fill=_GREY, width=1)
        else:
            d.line([(plot_l, plot_b), (plot_r, plot_b)], fill=_INK, width=2)

        n = len(pts)
        step = plot_w / (n - 1)
        coords = [(plot_l + step * i, y(v)) for i, (_, v) in enumerate(pts)]
        # line
        d.line(coords, fill=_GREEN, width=4, joint="curve")
        af = _font(18)
        vf = _font(16)
        for i, ((lbl, val), (cx, cy)) in enumerate(zip(pts, coords)):
            col = _GREEN if val >= 0 else _RED
            d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=col)
            vt = _naira_short(val)
            d.text((cx - _text_w(d, vt, vf) / 2, cy - 26), vt, fill=_INK, font=vf)
            lt = lbl if len(lbl) <= 10 else lbl[:9] + "…"
            d.text((cx - _text_w(d, lt, af) / 2, plot_b + 12), lt, fill=_GREY, font=af)
        return _save(img, filename)
    except Exception as e:
        logger.error(f"trend_chart failed: {e}")
        return None
