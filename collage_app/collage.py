"""Collage engine.

Pure Pillow, no Django imports, so it can be tested and reused on its own.

Pipeline
    1. Photo()          -> cheap metadata read (size + EXIF orientation), no pixel decode
    2. _layout()        -> geometry only: which photo goes where on which page
    3. build_collage()  -> decode each photo once at its final size and paste it
    4. images_to_pdf()  -> wrap the page JPEGs in a PDF without re-encoding them
"""

import os
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

HEIF_SUPPORTED = False
HEIF_ERROR = ""
try:  # lets iPhone HEIC/HEIF photos open; the app still works without it
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception as _exc:  # not installed, or its native library failed to load
    HEIF_ERROR = f"{type(_exc).__name__}: {_exc}"[:200]

DPI = 150
BASE_PAGE_WIDTH = 1240  # A4 width in px at 150 DPI; sizes below are scaled from it
PAGE_SIZES = {
    "A4": (1240, 1754),
    "A3": (1754, 2480),
    "Letter": (1275, 1650),
}
DEFAULT_PAGE_SIZE = "A4"
DEFAULT_MARGIN = 15
MAX_MARGIN = 60
JPEG_QUALITY = 90
MAX_PDF_PAGE_PIXELS = 20_000_000

_ORIENTATION_TAG = 0x0112
_ROTATED_ORIENTATIONS = {5, 6, 7, 8}


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

def _to_rgb(im):
    """Convert to RGB; transparent areas become white (not black)."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return im.convert("RGB")


class Photo:
    """Lightweight handle on an uploaded image. Pixels are only decoded in render()."""

    def __init__(self, name, fileobj):
        self.name = name
        self._file = fileobj

        fileobj.seek(0)
        with Image.open(fileobj) as im:
            im.verify()  # raises on corrupt / non-image data

        fileobj.seek(0)
        with Image.open(fileobj) as im:
            width, height = im.size
            orientation = im.getexif().get(_ORIENTATION_TAG, 1)

        if orientation in _ROTATED_ORIENTATIONS:
            width, height = height, width
        if width < 1 or height < 1:
            raise ValueError("empty image")
        self.size = (width, height)
        self.aspect = width / height

    def render(self, width, height):
        """Return the photo as an RGB image of exactly width x height, upright."""
        self._file.seek(0)
        with Image.open(self._file) as im:
            side = max(width, height)
            im.draft("RGB", (side, side))  # JPEG: decode at reduced size (much faster)
            im = ImageOps.exif_transpose(im)
            im = _to_rgb(im)
            return im.resize((width, height), Image.Resampling.LANCZOS, reducing_gap=3.0)


# --------------------------------------------------------------------------
# Fonts (header / footer text)
# --------------------------------------------------------------------------

_FONT_DIR = Path(__file__).resolve().parent / "fonts"

# Latin-friendly fonts first, then fonts with broad script coverage (Hindi, Bangla ...).
# Plain file names are resolved by Pillow in the usual system font folders.
_SYSTEM_FONTS = [
    "arial.ttf",
    "Arial.ttf",
    "DejaVuSans.ttf",
    "NotoSans-Regular.ttf",
    "NotoSansDevanagari-Regular.ttf",
    "NotoSansBengali-Regular.ttf",
    "Nirmala.ttf",
    "Nirmala.ttc",
    "Nirmala UI.ttf",
    "FreeSans.ttf",
    "Arial Unicode.ttf",
    "mangal.ttf",
    "vrinda.ttf",
    "Helvetica.ttc",
]


def _candidate_fonts():
    custom = os.environ.get("COLLAGE_FONT")
    if custom:
        yield custom
    if _FONT_DIR.is_dir():
        for path in sorted(_FONT_DIR.iterdir()):
            if path.suffix.lower() in (".ttf", ".otf", ".ttc"):
                yield str(path)
    yield from _SYSTEM_FONTS


def _glyph_signature(font, char):
    canvas = Image.new("L", (120, 120), 0)
    ImageDraw.Draw(canvas).text((10, 10), char, font=font, fill=255)
    return canvas.tobytes()


def _supports(font, text):
    """True if the font has a real glyph for every visible character in text."""
    chars = {c for c in text if not c.isspace()}
    if not chars:
        return True
    missing = _glyph_signature(font, "￿")  # an unmapped code point draws .notdef
    return all(_glyph_signature(font, c) != missing for c in chars)


def _pick_font(text):
    """Return (path_or_None, supports_all_characters)."""
    first_loadable = None
    for path in _candidate_fonts():
        try:
            font = ImageFont.truetype(path, 40)
        except OSError:
            continue
        if first_loadable is None:
            first_loadable = path
        if _supports(font, text):
            return path, True
    return first_loadable, False


def _make_font(path, size):
    if path:
        return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _fit_font(path, text, base_size, max_width):
    """Largest font (<= base_size) whose rendering of text fits max_width."""
    size = base_size
    while True:
        font = _make_font(path, size)
        if size <= 12 or font.getlength(text) <= max_width:
            return font
        size -= 2


def _draw_banner(page, text, zone_top, zone_height, background, color, scale):
    path, supported = _pick_font(text)
    font = _fit_font(path, text, round(35 * scale), page.width - round(80 * scale))
    draw = ImageDraw.Draw(page)

    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = (page.width - (right - left)) // 2 - left
    y = zone_top + (zone_height - (bottom - top)) // 2 - top

    pad_x, pad_top, pad_bottom = round(5 * scale), round(8 * scale), round(5 * scale)
    draw.rectangle(
        [x + left - pad_x, y + top - pad_top, x + right + pad_x, y + bottom + pad_bottom],
        fill=background,
    )
    draw.text((x, y), text, fill=color, font=font)
    return supported


def _banner_layer(size, header_text, footer_text, header_h, footer_h):
    """Blank white page with header/footer already drawn. Copied once per page."""
    layer = Image.new("RGB", size, "white")
    scale = size[0] / BASE_PAGE_WIDTH
    warnings = []
    if header_text and not _draw_banner(layer, header_text, 0, header_h, "#ffeb3b", "black", scale):
        warnings.append("Header text has characters your installed fonts cannot draw.")
    if footer_text and not _draw_banner(
        layer, footer_text, size[1] - footer_h, footer_h, "#f3f4f6", "#374151", scale
    ):
        warnings.append("Footer text has characters your installed fonts cannot draw.")
    return layer, warnings


# --------------------------------------------------------------------------
# Layout (geometry only)
# --------------------------------------------------------------------------

@dataclass
class Placement:
    idx: int  # index into the photo list
    x: int
    y: int
    w: int
    h: int


def _plan_rows(ids, aspects, target_h, width, gap, max_h):
    """Split photos (in order) into justified rows close to target_h tall.

    Returns [(ids_in_row, row_height, is_partial)]. Every complete row spans the
    full width. Only the final row can be partial (not enough photos to fill it);
    it is kept at target_h instead of being stretched.
    """

    def raw_height(count, aspect_sum):
        return (width - gap * (count - 1)) / aspect_sum

    rows = []
    row, aspect_sum = [], 0.0
    for i in ids:
        a = aspects[i]
        if row:
            h_without = raw_height(len(row), aspect_sum)
            h_with = raw_height(len(row) + 1, aspect_sum + a)
            # Adding this photo would make the row shorter than the target.
            # Close the row first if that lands nearer the target height.
            if h_with < target_h and (h_without - target_h) <= (target_h - h_with) and h_without <= target_h * 1.25:
                rows.append((row, min(h_without, max_h), False))
                row, aspect_sum = [], 0.0
        row.append(i)
        aspect_sum += a
        h = raw_height(len(row), aspect_sum)
        if h <= target_h:
            rows.append((row, min(h, max_h), False))
            row, aspect_sum = [], 0.0
    if row:
        rows.append((row, min(float(target_h), max_h), True))
    return rows


def _paginate(rows, usable_h, gap):
    pages, current, used = [], [], 0.0
    for row in rows:
        need = row[1] + (gap if current else 0)
        if current and used + need > usable_h:
            pages.append(current)
            current, used, need = [], 0.0, row[1]
        current.append(row)
        used += need
    if current:
        pages.append(current)
    return pages


def _layout(aspects, page_w, page_h, margin, header_h, footer_h):
    """Return pages -> list[Placement]."""
    count = len(aspects)
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - header_h - footer_h
    top = margin + header_h
    gap = margin
    scale = page_w / BASE_PAGE_WIDTH

    h_lo = max(40, round(220 * scale))
    h_hi = usable_h if count == 1 else min(usable_h, round(800 * scale))
    h_hi = max(h_hi, h_lo)
    ids = list(range(count))

    def paged(sub_ids, target_h):
        rows = _plan_rows(sub_ids, aspects, target_h, usable_w, gap, usable_h)
        return _paginate(rows, usable_h, gap)

    # Fewest pages possible with reasonably small photos ...
    min_pages = len(paged(ids, h_lo))
    # ... then the biggest photos that still fit in that many pages.
    chosen_h, pages = h_lo, paged(ids, h_lo)
    for target_h in range(int(h_hi), int(h_lo), -4):
        candidate = paged(ids, target_h)
        if len(candidate) <= min_pages:
            chosen_h, pages = target_h, candidate
            break

    # Pages before the last: enlarge photos a little so the page is filled better.
    refined = []
    for k, page_rows in enumerate(pages):
        if k < len(pages) - 1:
            page_ids = [i for row_ids, _, _ in page_rows for i in row_ids]
            for target_h in range(min(int(h_hi), int(chosen_h * 1.2)), int(chosen_h), -2):
                candidate = paged(page_ids, target_h)
                if len(candidate) == 1 and not candidate[0][-1][2]:
                    page_rows = candidate[0]
                    break
        refined.append(page_rows)

    result = []
    last = len(refined) - 1
    for k, rows in enumerate(refined):
        total = sum(h for _, h, _ in rows) + gap * (len(rows) - 1)
        slack = max(0.0, usable_h - total)
        extra = 0.0
        if k < last and len(rows) > 1:
            extra = min(slack / (len(rows) - 1), gap * 2)
        total += extra * (len(rows) - 1)
        slack = max(0.0, usable_h - total)
        y = top + (slack / 2 if (k < last or last == 0) else 0.0)

        placements = []
        for row_ids, h, _ in rows:
            row_w = sum(aspects[i] * h for i in row_ids) + gap * (len(row_ids) - 1)
            x = margin + max(0.0, (usable_w - row_w) / 2)
            y0, y1 = round(y), round(y + h)
            for i in row_ids:
                w = aspects[i] * h
                x0, x1 = round(x), round(x + w)
                placements.append(Placement(i, x0, y0, max(1, x1 - x0), max(1, y1 - y0)))
                x += w + gap
            y += h + gap + extra
        result.append(placements)
    return result


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

@dataclass
class Collage:
    pages: list = field(default_factory=list)  # PIL images
    skipped: list = field(default_factory=list)  # names of unreadable files
    skipped_reasons: list = field(default_factory=list)  # why, same order as skipped
    warnings: list = field(default_factory=list)


def _skip_reason(name, exc):
    if name.lower().endswith((".heic", ".heif")) and not HEIF_SUPPORTED:
        if HEIF_ERROR.startswith("ModuleNotFoundError"):
            return (
                "pillow-heif is not installed for the Python this server runs on "
                f"({sys.executable}). Run: pip install pillow-heif with that Python, then restart the server"
            )
        return f"HEIC support failed to load ({HEIF_ERROR}). Server Python: {sys.executable}"
    if isinstance(exc, UnidentifiedImageError):
        return "not a readable image"
    return f"could not be read as an image ({type(exc).__name__}: {str(exc).strip()[:100]})"


def build_collage(sources, page_size=DEFAULT_PAGE_SIZE, margin=DEFAULT_MARGIN,
                  header_text="", footer_text=""):
    """sources: iterable of (name, seekable file object). Photo order is kept."""
    result = Collage()
    if page_size not in PAGE_SIZES:
        page_size = DEFAULT_PAGE_SIZE
    margin = max(0, min(int(margin), MAX_MARGIN))
    page_w, page_h = PAGE_SIZES[page_size]

    photos = []
    for name, fileobj in sources:
        try:
            photos.append(Photo(name, fileobj))
        except Exception as exc:
            result.skipped.append(name)
            result.skipped_reasons.append(_skip_reason(name, exc))
    if not photos:
        return result

    scale = page_w / BASE_PAGE_WIDTH
    header_h = round(80 * scale) if header_text else 0
    footer_h = round(70 * scale) if footer_text else 0
    layer, result.warnings = _banner_layer((page_w, page_h), header_text, footer_text, header_h, footer_h)

    layout = _layout([p.aspect for p in photos], page_w, page_h, margin, header_h, footer_h)
    for placements in layout:
        page = layer.copy()
        for p in placements:
            try:
                tile = photos[p.idx].render(p.w, p.h)
            except Exception:
                tile = Image.new("RGB", (p.w, p.h), "#e5e7eb")
                result.warnings.append(f"{photos[p.idx].name} could not be fully read.")
            page.paste(tile, (p.x, p.y))
            tile.close()
        result.pages.append(page)
    return result


def jpeg_bytes(image):
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True, dpi=(DPI, DPI))
    return buffer.getvalue()


def _prepare_pdf_page(data, number):
    """Return (jpeg_bytes, width_px, height_px) ready to embed."""
    try:
        with Image.open(BytesIO(data)) as im:
            width, height = im.size
            if width * height > MAX_PDF_PAGE_PIXELS:
                raise ValueError(f"Page {number} is too large.")
            if im.format == "JPEG" and im.mode == "RGB" and "adobe" not in im.info:
                return data, width, height  # embed as-is: no quality loss, no re-encode
            converted = _to_rgb(ImageOps.exif_transpose(im))
    except ValueError:
        raise
    except Exception:
        raise ValueError(f"Page {number} is not a valid image.") from None
    return jpeg_bytes(converted), converted.width, converted.height


def images_to_pdf(page_images):
    """Build a PDF from page images (bytes). JPEGs are embedded without re-encoding."""
    pages = [_prepare_pdf_page(data, i + 1) for i, data in enumerate(page_images)]
    if not pages:
        raise ValueError("No pages to put in the PDF.")

    out = BytesIO()
    offsets = {}

    def add_object(number, body):
        offsets[number] = out.tell()
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")

    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    add_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 3 * i} 0 R" for i in range(len(pages)))
    add_object(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())

    for i, (data, width, height) in enumerate(pages):
        page_obj, content_obj, image_obj = 3 + 3 * i, 4 + 3 * i, 5 + 3 * i
        w_pt, h_pt = width * 72 / DPI, height * 72 / DPI
        add_object(
            page_obj,
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w_pt:.2f} {h_pt:.2f}] "
                f"/Resources << /XObject << /Im0 {image_obj} 0 R >> >> /Contents {content_obj} 0 R >>"
            ).encode(),
        )
        content = f"q {w_pt:.2f} 0 0 {h_pt:.2f} 0 0 cm /Im0 Do Q".encode()
        add_object(content_obj, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        add_object(
            image_obj,
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\nstream\n"
            ).encode()
            + data
            + b"\nendstream",
        )

    total = 3 + 3 * len(pages)
    xref_at = out.tell()
    out.write(b"xref\n0 %d\n" % total)
    out.write(b"0000000000 65535 f \n")
    for number in range(1, total):
        out.write(b"%010d 00000 n \n" % offsets[number])
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (total, xref_at))
    return out.getvalue()
