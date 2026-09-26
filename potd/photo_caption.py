"""Text and layout for the photo info written in the bottom-left corner of a Photos wallpaper.
Pure Python (no macOS frameworks) so it can be unit-tested anywhere."""
from __future__ import annotations


def _clean(x) -> str:
    return " ".join(str(x or "").replace("\x00", " ").split())


def format_camera(make, model) -> str:
    """'Apple' + 'iPhone 15 Pro' -> 'Apple iPhone 15 Pro'; 'Canon' + 'Canon EOS R6' -> 'Canon EOS R6';
    'NIKON CORPORATION' + 'NIKON Z 6' -> 'NIKON Z 6'; 'SONY' + 'ILCE-7M3' -> 'Sony ILCE-7M3'."""
    make, model = _clean(make), _clean(model)
    if not model or not make:
        return model or make
    brand = make.split()[0]                      # "NIKON CORPORATION" -> "NIKON"
    if model.lower().startswith(brand.lower()):
        return model
    if brand.isupper() and len(brand) > 3:       # "SONY" -> "Sony" (keep "HTC", "LG")
        brand = brand.capitalize()
    return f"{brand} {model}"


def format_place(locality=None, area=None, country=None, name=None) -> str:
    """'Brussels', 'Brussels', 'Belgium' -> 'Brussels, Belgium'."""
    parts = []
    for p in (locality or name, area, country):
        p = _clean(p)
        if p and p.lower() not in (x.lower() for x in parts):
            parts.append(p)
    if len(parts) == 3:                 # locality, region, country -> keep it short
        parts = [parts[0], parts[2]]
    return ", ".join(parts)


def coordinate_values(coord) -> tuple[float, float]:
    """(latitude, longitude) from a CLLocationCoordinate2D, which PyObjC may give as a
    struct with .latitude/.longitude or as a plain (latitude, longitude) tuple."""
    if hasattr(coord, "latitude"):
        return float(coord.latitude), float(coord.longitude)
    lat, lon = coord[0], coord[1]
    return float(lat), float(lon)


def format_coordinates(lat: float, lon: float) -> str:
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"


def caption_lines(date_text: str, place: str, camera: str) -> list[str]:
    """Top-to-bottom lines; missing information is simply left out."""
    return [x for x in (_clean(date_text), _clean(place), _clean(camera)) if x]


def fill_rect(img_w: float, img_h: float, out_w: float, out_h: float):
    """Where to draw an image so it covers the whole output (centre crop, 'aspect fill').
    Returns (x, y, w, h) in output coordinates."""
    scale = max(out_w / img_w, out_h / img_h)
    w, h = img_w * scale, img_h * scale
    return (out_w - w) / 2, (out_h - h) / 2, w, h


def fit_rect(img_w: float, img_h: float, out_w: float, out_h: float):
    """Where to draw an image so all of it is visible, centred ('aspect fit').
    Returns (x, y, w, h) in output coordinates."""
    scale = min(out_w / img_w, out_h / img_h)
    w, h = img_w * scale, img_h * scale
    return (out_w - w) / 2, (out_h - h) / 2, w, h


def screen_shapes(sizes, max_side: int = 6144) -> list[tuple[int, int]]:
    """Distinct screen pixel sizes, largest first (each gets its own version of a photo)."""
    out = []
    for w, h in sizes:
        w, h = int(w), int(h)
        if w <= 0 or h <= 0:
            continue
        k = min(1.0, max_side / max(w, h))
        size = (int(w * k), int(h * k))
        if size not in out:
            out.append(size)
    return sorted(out, key=lambda s: s[0] * s[1], reverse=True)


def best_variant(available, screen, tolerance: float = 0.02):
    """Which rendered size to show on a screen: exact size, else the same shape
    (aspect ratio within 2 %), largest first; None if no version has that shape."""
    screen = (int(screen[0]), int(screen[1]))
    if screen in available:
        return screen
    target = screen[0] / screen[1]
    same = [s for s in available if abs(s[0] / s[1] - target) / target <= tolerance]
    return max(same, key=lambda s: s[0] * s[1]) if same else None


def output_size(img_w: int, img_h: int, screen_w: int, screen_h: int) -> tuple[int, int]:
    """Screen-shaped output; smaller than the screen only if the photo is too small
    (never upscale), so macOS doesn't need to crop and the corner text stays visible."""
    need = max(screen_w / img_w, screen_h / img_h)
    if need > 1:
        return max(1, int(screen_w / need)), max(1, int(screen_h / need))
    return int(screen_w), int(screen_h)


def text_metrics(out_h: int) -> dict:
    """Font size and margins relative to the output height (same look on every screen)."""
    font = max(12, round(out_h / 62))
    return {"font": font, "margin_x": round(out_h * 0.03), "margin_y": round(out_h * 0.04),
            "pad": round(font * 0.6), "line_gap": round(font * 0.3)}


# ---------------------------------------------------------------- Liquid Glass look
# A picture file can't contain the live system material, so the caption panel recreates
# its look: frosted (blurred, brighter, more saturated) photo behind a rounded panel, a
# light tint, a bright rim along the top edge, a soft shadow, and text that switches
# between light and dark with the brightness behind it (like Liquid Glass does).

def relative_luminance(r: float, g: float, b: float) -> float:
    """sRGB components 0..1 -> perceived brightness 0..1 (WCAG formula)."""
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def glass_style(luminance: float) -> dict:
    """Panel and text colours for the brightness of the photo behind the panel."""
    if luminance > 0.45:        # bright sky, snow, beach: dark text on clear glass
        return {"text": (0.08, 0.9), "text_shadow": False, "tint": (1.0, 0.28),
                "brightness": 0.06, "saturation": 1.25, "rim": (1.0, 0.75)}
    if luminance > 0.18:        # mid tones
        return {"text": (1.0, 0.97), "text_shadow": True, "tint": (1.0, 0.12),
                "brightness": 0.02, "saturation": 1.35, "rim": (1.0, 0.6)}
    return {"text": (1.0, 0.97), "text_shadow": True, "tint": (1.0, 0.08),   # dark photos
            "brightness": 0.05, "saturation": 1.4, "rim": (1.0, 0.45)}


def glass_geometry(text_w: float, text_h: float, m: dict) -> dict:
    """Panel rectangle (x, y, w, h from the bottom-left), corner radius, blur and rim width."""
    pad_x, pad_y = round(m["font"] * 0.9), round(m["font"] * 0.65)
    w, h = text_w + 2 * pad_x, text_h + 2 * pad_y
    return {"x": m["margin_x"], "y": m["margin_y"], "w": w, "h": h,
            "text_x": m["margin_x"] + pad_x, "text_y": m["margin_y"] + pad_y,
            "radius": min(h / 2, m["font"] * 1.25),   # generous, rounded like system panels
            "blur": max(6.0, m["font"] * 0.9), "rim": max(1.0, m["font"] / 22),
            "shadow_blur": m["font"] * 0.9, "shadow_y": -m["font"] * 0.18}
