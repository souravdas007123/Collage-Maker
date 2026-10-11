"""Collage engine.

Pure Pillow, no Django imports, so it can be tested and reused on its own.

Pipeline
    1. Photo()          -> cheap metadata read (size + EXIF orientation), no pixel decode
    2. _layout()        -> geometry only: which photo goes where on which page
    3. build_collage()  -> decode each photo once at its final size and paste it
    4. images_to_pdf()  -> wrap the page JPEGs in a PDF without re-encoding them
"""

import math
import os
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

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

SIGNATURE_POSITIONS = ("left", "center", "right")
SIGNATURE_PAGES = ("last", "all")
DEFAULT_SIGNATURE_HEIGHT = 100  # px, at A4 scale
MIN_SIGNATURE_HEIGHT = 40
MAX_SIGNATURE_HEIGHT = 200

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

    def render(self, width, height, rotate=False):
        """Return the photo as an RGB image of exactly width x height.

        rotate=True turns it 90 degrees clockwise; width x height is then the size
        AFTER turning (so the photo is first drawn upright at height x width).
        """
        if rotate:
            width, height = height, width
        self._file.seek(0)
        with Image.open(self._file) as im:
            side = max(width, height)
            im.draft("RGB", (side, side))  # JPEG: decode at reduced size (much faster)
            im = ImageOps.exif_transpose(im)
            im = _to_rgb(im)
            im = im.resize((width, height), Image.Resampling.LANCZOS, reducing_gap=3.0)
        return im.transpose(Image.Transpose.ROTATE_270) if rotate else im


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
# Signature stamp
# --------------------------------------------------------------------------

def _remove_paper(rgb):
    """Turn a photo/scan of a signature on paper into dark ink on pure white.

    The paper brightness is estimated locally (so shadows and uneven light are
    ignored); only pixels clearly darker than their surroundings are kept.
    """
    gray = rgb.convert("L")
    small = gray.resize((max(1, gray.width // 8), max(1, gray.height // 8)), Image.Resampling.BILINEAR)
    small = small.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(2))
    paper = small.resize(gray.size, Image.Resampling.BILINEAR)
    darker = ImageChops.subtract(paper, gray)
    alpha = darker.point(lambda v: 0 if v < 18 else min(255, (v - 18) * 255 // 80))
    return Image.composite(rgb, Image.new("RGB", rgb.size, "white"), alpha)


def load_signature(fileobj, clean=True):
    """Return the signature as an RGB image: just the ink on white, cropped to the ink.

    Transparent PNGs (e.g. drawn in the browser) are flattened onto white. For
    photos/scans, clean=True removes the paper background first.
    """
    fileobj.seek(0)
    with Image.open(fileobj) as source:
        source.draft("RGB", (2000, 2000))
        im = ImageOps.exif_transpose(source)
        im.thumbnail((2000, 2000))
        has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
        rgb = _to_rgb(im)
    if clean and not has_alpha:
        rgb = _remove_paper(rgb)

    ink = ImageChops.difference(rgb, Image.new("RGB", rgb.size, "white")).convert("L")
    box = ink.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box:
        raise ValueError("The signature image has no visible ink.")
    pad = 4
    box = (max(0, box[0] - pad), max(0, box[1] - pad), min(rgb.width, box[2] + pad), min(rgb.height, box[3] + pad))
    return rgb.crop(box)


def _fit_signature(signature, height, page_w):
    """Scale to the requested height, but never wider than 45% of the page."""
    width = max(1, round(signature.width * height / signature.height))
    max_width = round(page_w * 0.45)
    if width > max_width:
        width = max_width
        height = max(1, round(signature.height * width / signature.width))
    return signature.resize((width, height), Image.Resampling.LANCZOS)


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


def _dp_rows(ids, aspects, width, gap, limit, band_lo, band_hi, max_per_row=10):
    """Exact row packing for ONE page.

    Splits the photos (order kept) into full-width rows, each between band_lo and
    band_hi tall, so that the total height is as large as possible without going
    over `limit`. Photos keep their aspect ratio; nothing is cropped.
    Returns (rows, total_height) or None when no split fits.
    """
    count = len(ids)
    limit = int(limit)
    mask = (1 << (limit + 1)) - 1
    prefix = [0.0]
    for i in ids:
        prefix.append(prefix[-1] + aspects[i])

    def row_height(j, i):
        return (width - gap * (i - j - 1)) / (prefix[i] - prefix[j])

    # reach[i] is a bit set: bit t is on when the first i photos can be laid out in exactly t px
    reach = [0] * (count + 1)
    reach[0] = 1
    for i in range(1, count + 1):
        bits = 0
        for j in range(max(0, i - max_per_row), i):
            if not reach[j]:
                continue
            h = row_height(j, i)
            if h < band_lo or h > band_hi:
                continue
            bits |= (reach[j] << (math.ceil(h) + (gap if j else 0))) & mask
        reach[i] = bits
    if not reach[count]:
        return None

    total = reach[count].bit_length() - 1
    rows, i, t = [], count, total
    middle = (band_lo + band_hi) / 2
    while i > 0:
        best = None
        for j in range(max(0, i - max_per_row), i):
            h = row_height(j, i)
            if h < band_lo or h > band_hi:
                continue
            step = math.ceil(h) + (gap if j else 0)
            if t - step >= 0 and (reach[j] >> (t - step)) & 1:
                if best is None or abs(h - middle) < abs(best[1] - middle):
                    best = (j, h, step)
        j, h, step = best
        rows.append((ids[j:i], h, False))
        i, t = j, t - step
    rows.reverse()
    return rows, total


def _dp_best(ids, aspects, width, gap, limit, ref_hi, ref_lo, step, floor):
    """Pick the exact packing that uses the page best.

    First tries rows of similar height (neat). Only if that leaves the page less than
    ~93% used does it allow bigger differences between rows (e.g. one large photo
    plus smaller ones), and only when that clearly fills more of the page.
    """

    def scan(low_factor, high_factor):
        best, ref = None, ref_hi
        while ref >= ref_lo:
            found = _dp_rows(ids, aspects, width, gap, limit, max(floor, ref * low_factor), min(limit, ref * high_factor))
            if found and (best is None or found[1] > best[1]):
                best = found
            ref -= step
        return best

    best = scan(0.72, 1.28)
    if best is None or best[1] < 0.93 * limit:
        for low_factor, high_factor in ((0.55, 1.7), (0.3, 3.0)):
            wider = scan(low_factor, high_factor)
            if wider and (best is None or wider[1] > best[1] + 0.04 * limit):
                best = wider
    return best


def _layout(aspects, page_w, page_h, margin, header_h, footer_h):
    """Return pages -> list[Placement]. Everything is packed from the top of the page down."""
    count = len(aspects)
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - header_h - footer_h
    top = margin + header_h
    gap = margin
    scale = page_w / BASE_PAGE_WIDTH

    h_lo = max(40, round(180 * scale))
    h_hi = usable_h if count == 1 else min(usable_h, round(800 * scale))
    h_hi = max(h_hi, h_lo)
    ids = list(range(count))
    step = max(4, round(12 * scale))

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

    def used(rows):
        return sum(h for _, h, _ in rows) + gap * (len(rows) - 1)

    # Everything on ONE page whenever an exact packing exists, and use the page as fully as possible.
    single = _dp_best(ids, aspects, usable_w, gap, usable_h, h_hi, h_lo, step, h_lo)
    if single and (len(pages) > 1 or single[1] > used(pages[0])):
        pages = [single[0]]

    # Pages before the last: repack exactly so the page is filled as far down as possible.
    refined = []
    for k, page_rows in enumerate(pages):
        if k < len(pages) - 1:
            page_ids = [i for row_ids, _, _ in page_rows for i in row_ids]
            better = _dp_best(page_ids, aspects, usable_w, gap, usable_h,
                              min(h_hi, chosen_h * 1.3), chosen_h * 0.85, max(2, step // 2), h_lo)
            if better and better[1] > used(page_rows):
                page_rows = better[0]
        refined.append(page_rows)

    result = []
    for rows in refined:
        y = float(top)
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
            y += h + gap
        result.append(placements)
    return result


def _coverage(layout, usable_w, usable_h):
    """Share of the printable area (all pages) that is covered by photos."""
    covered = sum(p.w * p.h for page in layout for p in page)
    return covered / (len(layout) * usable_w * usable_h)


def _best_layout(aspects, page_w, page_h, margin, header_h, footer_h, auto_rotate):
    """Return (layout, rotated_flags). Tries turning photos 90 degrees if that fills the page better."""
    usable_w = page_w - 2 * margin
    usable_h = page_h - 2 * margin - header_h - footer_h

    def run(flags):
        turned = [1 / a if flag else a for a, flag in zip(aspects, flags)]
        return _layout(turned, page_w, page_h, margin, header_h, footer_h)

    upright = [False] * len(aspects)
    best_layout = run(upright)
    best_flags = upright
    if not auto_rotate:
        return best_layout, best_flags

    base_pages = len(best_layout)
    base_cover = _coverage(best_layout, usable_w, usable_h)
    best_key = (base_pages, -base_cover)
    for flags in ([a > 1.05 for a in aspects], [a < 0.95 for a in aspects]):  # all -> portrait, all -> landscape
        if not any(flags):
            continue
        layout = run(flags)
        cover = _coverage(layout, usable_w, usable_h)
        better = len(layout) < base_pages or (len(layout) == base_pages and cover >= base_cover + 0.05)
        if better and (len(layout), -cover) < best_key:
            best_layout, best_flags, best_key = layout, flags, (len(layout), -cover)
    return best_layout, best_flags


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

@dataclass
class Collage:
    pages: list = field(default_factory=list)  # PIL images
    skipped: list = field(default_factory=list)  # names of unreadable files
    skipped_reasons: list = field(default_factory=list)  # why, same order as skipped
    warnings: list = field(default_factory=list)
    rotated: int = 0  # photos turned 90 degrees to fill the page better


def _skip_reason(name, exc):
    if name.lower().endswith((".heic", ".heif")) and not HEIF_SUPPORTED:
        if HEIF_ERROR.startswith("ModuleNotFoundError"):
            return (
                "pillow-heif is not installed for the Python this server runs on "
                f"({sys.executable}). Run: pip install pillow-heif with that Python, then restart the server"
            )
        if "DLL load failed" in HEIF_ERROR or "Application Control" in HEIF_ERROR:
            return (
                "Windows blocked the HEIC library on this computer. The browser normally converts HEIC "
                "photos itself; if that failed, save the photo as JPG and add it again"
            )
        return f"HEIC support failed to load ({HEIF_ERROR}). Server Python: {sys.executable}"
    if isinstance(exc, UnidentifiedImageError):
        return "not a readable image"
    return f"could not be read as an image ({type(exc).__name__}: {str(exc).strip()[:100]})"


def build_collage(sources, page_size=DEFAULT_PAGE_SIZE, margin=DEFAULT_MARGIN,
                  header_text="", footer_text="", signature=None, signature_position="right",
                  signature_height=DEFAULT_SIGNATURE_HEIGHT, signature_pages="last",
                  signature_clean=True, auto_rotate=True):
    """sources: iterable of (name, seekable file object). Photo order is kept.

    signature: optional seekable image file. It is stamped in its own band above the
    footer text, so it never covers a photo. Every page keeps the same margins.
    """
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
    footer_text_h = round(70 * scale) if footer_text else 0

    stamp, stamp_band = None, 0
    if signature is not None:
        try:
            height = max(MIN_SIGNATURE_HEIGHT, min(int(signature_height), MAX_SIGNATURE_HEIGHT))
            stamp = _fit_signature(load_signature(signature, clean=signature_clean), round(height * scale), page_w)
            stamp_band = stamp.height + 2 * round(10 * scale)
        except Exception:
            result.warnings.append("The signature image could not be read, so it was not added.")
    if signature_position not in SIGNATURE_POSITIONS:
        signature_position = "right"
    footer_h = footer_text_h + stamp_band

    layer, banner_warnings = _banner_layer((page_w, page_h), header_text, footer_text, header_h, footer_text_h)
    result.warnings = banner_warnings + result.warnings

    stamp_xy = None
    if stamp:
        side = max(margin, round(30 * scale))
        x = {"left": side, "center": (page_w - stamp.width) // 2, "right": page_w - side - stamp.width}[signature_position]
        stamp_xy = (x, page_h - footer_h + (stamp_band - stamp.height) // 2)

    layout, rotated = _best_layout([p.aspect for p in photos], page_w, page_h, margin, header_h, footer_h, auto_rotate)
    result.rotated = sum(rotated)
    for index, placements in enumerate(layout):
        page = layer.copy()
        for p in placements:
            try:
                tile = photos[p.idx].render(p.w, p.h, rotate=rotated[p.idx])
            except Exception:
                tile = Image.new("RGB", (p.w, p.h), "#e5e7eb")
                result.warnings.append(f"{photos[p.idx].name} could not be fully read.")
            page.paste(tile, (p.x, p.y))
            tile.close()
        if stamp_xy and (signature_pages == "all" or index == len(layout) - 1):
            page.paste(stamp, stamp_xy)
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
