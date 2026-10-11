"""Tests for the collage engine. Run with:  python manage.py test   (or: python -m unittest)"""

import io
import re
import sys
import unittest

from PIL import Image, ImageChops, ImageDraw

from . import collage


def make_jpeg(width, height, orientation=None):
    image = Image.new("RGB", (width, height), (90, 140, 200))
    buffer = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        image.save(buffer, "JPEG", exif=exif)
    else:
        image.save(buffer, "JPEG")
    buffer.seek(0)
    return buffer


def sources(count, width=1600, height=1200):
    return [(f"{i}.jpg", make_jpeg(width, height)) for i in range(count)]


def make_signature(kind="paper"):
    """A fake signature: blue scribble on grey paper (JPEG), or dark ink on transparent PNG."""
    width, height = 700, 300
    if kind == "png":
        image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ink = (17, 24, 39, 255)
    elif kind == "blank":
        image = Image.new("RGB", (width, height), (230, 230, 226))
        ink = None
    else:
        image = Image.new("RGB", (width, height), (215, 215, 210))
        ink = (20, 40, 150)
    if ink:
        draw = ImageDraw.Draw(image)
        draw.line([(50 + i * 8, 150 + (40 if i % 2 else -40)) for i in range(76)], fill=ink, width=6)
    buffer = io.BytesIO()
    image.save(buffer, "PNG" if kind == "png" else "JPEG")
    buffer.seek(0)
    return buffer


def ink_box(image):
    """Bounding box of everything that is not white."""
    return ImageChops.difference(image.convert("RGB"), Image.new("RGB", image.size, "white")).getbbox()


class SignatureTests(unittest.TestCase):
    def test_paper_background_is_removed_and_cropped_to_ink(self):
        sig = collage.load_signature(make_signature("paper"))
        self.assertEqual(sig.getpixel((1, 1)), (255, 255, 255))
        self.assertLess(sig.width, 700)  # cropped, not the whole paper

    def test_transparent_png_is_flattened_onto_white(self):
        sig = collage.load_signature(make_signature("png"))
        self.assertEqual(sig.getpixel((1, 1)), (255, 255, 255))
        self.assertTrue(any(sum(sig.getpixel((x, y))) < 150 for x in range(0, sig.width, 4) for y in range(0, sig.height, 4)))

    def test_blank_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            collage.load_signature(make_signature("blank"))

    def test_signature_goes_on_last_page_only_by_default(self):
        result = collage.build_collage(sources(40), auto_rotate=False, signature=make_signature("png"))
        self.assertGreaterEqual(len(result.pages), 2)
        bottom = lambda page: page.crop((0, page.height - 100, page.width, page.height))
        self.assertIsNone(ink_box(bottom(result.pages[0])))
        self.assertIsNotNone(ink_box(bottom(result.pages[-1])))

    def test_signature_on_every_page(self):
        result = collage.build_collage(sources(40), auto_rotate=False, signature=make_signature("png"), signature_pages="all")
        for page in result.pages:
            self.assertIsNotNone(ink_box(page.crop((0, page.height - 100, page.width, page.height))))

    def test_signature_position(self):
        centers = {}
        for position in ("left", "center", "right"):
            page = collage.build_collage(sources(2), auto_rotate=False, signature=make_signature("png"), signature_position=position).pages[0]
            box = ink_box(page.crop((0, page.height - 100, page.width, page.height)))
            centers[position] = (box[0] + box[2]) / 2
        self.assertLess(centers["left"], centers["center"])
        self.assertLess(centers["center"], centers["right"])
        self.assertAlmostEqual(centers["center"], 620, delta=15)

    def test_signature_sits_above_footer_text_and_below_photos(self):
        page = collage.build_collage(sources(2), auto_rotate=False, footer_text="Footer", signature=make_signature("png"), signature_height=100).pages[0]
        text_zone = page.crop((400, page.height - 70, 840, page.height))   # centred footer text
        self.assertIsNotNone(ink_box(text_zone))
        sig_zone = page.crop((900, page.height - 70 - 110, page.width, page.height - 70 - 10))
        self.assertIsNotNone(ink_box(sig_zone))

    def test_unreadable_signature_is_reported_and_collage_still_made(self):
        without = collage.build_collage(sources(3), auto_rotate=False)
        result = collage.build_collage(sources(3), auto_rotate=False, signature=make_signature("blank"))
        self.assertEqual(len(result.pages), len(without.pages))
        self.assertTrue(any("signature" in w.lower() for w in result.warnings))

    def test_signature_size_is_capped_to_page_width(self):
        wide = Image.new("RGB", (4000, 100), "white")
        ImageDraw.Draw(wide).line([(0, 50), (3999, 50)], fill="black", width=8)
        buffer = io.BytesIO()
        wide.save(buffer, "PNG")
        buffer.seek(0)
        page = collage.build_collage(sources(1), auto_rotate=False, signature=buffer, signature_height=200).pages[0]
        box = ink_box(page.crop((0, page.height - 210, page.width, page.height)))
        self.assertLessEqual(box[2] - box[0], round(page.width * 0.45) + 2)


def photo_with_red_corner(width, height):
    """A photo whose top-left corner is red, so rotation direction can be checked."""
    image = Image.new("RGB", (width, height), (40, 90, 160))
    ImageDraw.Draw(image).rectangle((0, 0, width // 5, height // 5), fill=(255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95)
    buffer.seek(0)
    return buffer


def used_height_fraction(page, margin=15):
    box = ink_box(page)
    return (box[3] - box[1]) / (page.height - 2 * margin)


class PageFillTests(unittest.TestCase):
    def test_no_header_or_footer_means_no_reserved_space(self):
        # 4 portrait photos: rows of two fill the page from top margin to bottom margin
        result = collage.build_collage([(f"{i}.jpg", make_jpeg(1200, 1600)) for i in range(4)])
        box = ink_box(result.pages[0])
        self.assertLessEqual(box[1], 15 + 70)                  # starts right at the top
        self.assertGreaterEqual(box[3], result.pages[0].height - 15 - 70)  # reaches the bottom

    def test_header_space_only_when_header_text_exists(self):
        without = collage.build_collage(sources(6))
        with_header = collage.build_collage(sources(6), header_text="Title")
        # the banner is drawn in the top 80 px only when there is header text
        self.assertIsNone(ink_box(without.pages[0].crop((0, 0, 1240, 10))))
        self.assertIsNotNone(ink_box(with_header.pages[0].crop((0, 0, 1240, 80))))

    def test_landscape_photos_are_turned_to_fill_a_portrait_page(self):
        upright = collage.build_collage(sources(4), auto_rotate=False)
        turned = collage.build_collage(sources(4))
        self.assertEqual(upright.rotated, 0)
        self.assertEqual(turned.rotated, 4)
        self.assertLess(used_height_fraction(upright.pages[0]), 0.7)
        self.assertGreater(used_height_fraction(turned.pages[0]), 0.95)

    def test_single_landscape_photo_fills_the_page(self):
        turned = collage.build_collage(sources(1))
        self.assertEqual(turned.rotated, 1)
        self.assertGreater(used_height_fraction(turned.pages[0]), 0.9)

    def test_rotation_is_clockwise(self):
        result = collage.build_collage([("a.jpg", photo_with_red_corner(1600, 1200))])
        page = result.pages[0]
        box = ink_box(page)
        red_top_right = page.getpixel((box[2] - 8, box[1] + 8))
        red_top_left = page.getpixel((box[0] + 8, box[1] + 8))
        self.assertGreater(red_top_right[0], 200)   # red corner moved to the top-right
        self.assertLess(red_top_left[0], 100)

    def test_photos_that_already_fit_are_not_turned(self):
        result = collage.build_collage([(f"{i}.jpg", make_jpeg(1200, 1600)) for i in range(4)])
        self.assertEqual(result.rotated, 0)

    def test_auto_rotate_never_needs_more_pages(self):
        for count in (1, 3, 7, 12, 25):
            for size in ((1600, 1200), (1200, 1600), (1500, 1500)):
                upright = collage.build_collage(sources(count, *size), auto_rotate=False)
                turned = collage.build_collage(sources(count, *size))
                self.assertLessEqual(len(turned.pages), len(upright.pages), (count, size))

    def test_leftover_height_is_spread_not_left_as_blank_bands(self):
        result = collage.build_collage(sources(6, 1600, 1200), auto_rotate=False)
        page = result.pages[0]
        box = ink_box(page)
        self.assertLess(box[1], 190)                 # photos start near the top ...
        self.assertGreater(page.height - box[3], 0)  # ... and the block is still inside the margins


class PhotoTests(unittest.TestCase):
    def test_exif_rotation_swaps_dimensions(self):
        self.assertEqual(collage.Photo("a.jpg", make_jpeg(400, 300, orientation=6)).size, (300, 400))
        self.assertEqual(collage.Photo("a.jpg", make_jpeg(400, 300, orientation=1)).size, (400, 300))

    def test_unreadable_file_is_skipped_not_fatal(self):
        result = collage.build_collage([("bad.txt", io.BytesIO(b"not an image"))] + sources(1))
        self.assertEqual(result.skipped, ["bad.txt"])
        self.assertEqual(len(result.pages), 1)

    def test_skip_reason_is_reported(self):
        result = collage.build_collage([("bad.txt", io.BytesIO(b"nope"))])
        self.assertEqual(len(result.skipped_reasons), 1)
        self.assertEqual(result.skipped_reasons[0], "not a readable image")

    def test_heic_without_support_says_how_to_fix_it(self):
        original = (collage.HEIF_SUPPORTED, collage.HEIF_ERROR)
        try:
            collage.HEIF_SUPPORTED, collage.HEIF_ERROR = False, "ModuleNotFoundError: No module named 'pillow_heif'"
            missing = collage.build_collage([("IMG_1.HEIC", io.BytesIO(b"nope"))])
            collage.HEIF_ERROR = "OSError: libheif not found"
            broken = collage.build_collage([("IMG_1.HEIC", io.BytesIO(b"nope"))])
        finally:
            collage.HEIF_SUPPORTED, collage.HEIF_ERROR = original
        self.assertIn("pip install pillow-heif", missing.skipped_reasons[0])
        self.assertIn("libheif not found", broken.skipped_reasons[0])
        self.assertIn(sys.executable, broken.skipped_reasons[0])

    def test_only_unreadable_files_gives_no_pages(self):
        result = collage.build_collage([("bad.txt", io.BytesIO(b"nope"))])
        self.assertEqual(result.pages, [])

    def test_transparent_png_becomes_white_not_black(self):
        png = io.BytesIO()
        Image.new("RGBA", (200, 200), (0, 0, 0, 0)).save(png, "PNG")
        png.seek(0)
        page = collage.build_collage([("t.png", png)]).pages[0]
        self.assertEqual(page.getpixel((page.width // 2, page.height // 2)), (255, 255, 255))


class LayoutTests(unittest.TestCase):
    def check_layout(self, aspects, margin=15, header_h=0, footer_h=0, size=(1240, 1754)):
        page_w, page_h = size
        pages = collage._layout(aspects, page_w, page_h, margin, header_h, footer_h)
        seen = []
        for placements in pages:
            for p in placements:
                seen.append(p.idx)
                self.assertGreaterEqual(p.x, margin)
                self.assertLessEqual(p.x + p.w, page_w - margin + 1)
                self.assertGreaterEqual(p.y, margin + header_h)
                self.assertLessEqual(p.y + p.h, page_h - margin - footer_h + 1)
            for a in placements:  # no two photos on a page overlap
                for b in placements:
                    if a is not b:
                        overlap_x = a.x < b.x + b.w and b.x < a.x + a.w
                        overlap_y = a.y < b.y + b.h and b.y < a.y + a.h
                        self.assertFalse(overlap_x and overlap_y, f"{a} overlaps {b}")
        self.assertEqual(seen, list(range(len(aspects))), "every photo placed once, in order")
        return pages

    def test_mixed_aspects_stay_inside_page_without_overlap(self):
        aspects = [1.5, 0.67, 1.0, 2.4, 0.5, 1.33, 0.75, 1.78, 0.56, 1.0, 3.0, 0.4] * 4
        self.check_layout(aspects)
        self.check_layout(aspects, margin=0)
        self.check_layout(aspects, header_h=80, footer_h=70)
        self.check_layout(aspects, size=collage.PAGE_SIZES["A3"])

    def test_single_photo(self):
        for aspect in (0.3, 0.75, 1.0, 1.5, 4.0):
            self.check_layout([aspect])

    def test_more_photos_never_means_fewer_pages_by_much(self):
        # Regression: the old formula gave 15 photos -> 3 pages but 20 photos -> 2 pages.
        counts = [len(collage._layout([1.33] * n, 1240, 1754, 15, 0, 0)) for n in (10, 15, 20, 30, 40)]
        self.assertEqual(counts, sorted(counts))

    def test_pages_are_well_filled(self):
        result = collage.build_collage(sources(30))
        for page in result.pages[:-1]:
            bbox = ImageChops.difference(page, Image.new("RGB", page.size, "white")).getbbox()
            self.assertGreater(bbox[3] / page.height, 0.85)


class OutputTests(unittest.TestCase):
    def test_page_sizes(self):
        for name, size in collage.PAGE_SIZES.items():
            page = collage.build_collage(sources(2), page_size=name).pages[0]
            self.assertEqual(page.size, size)

    def test_unknown_page_size_falls_back_to_a4(self):
        self.assertEqual(collage.build_collage(sources(1), page_size="Huge").pages[0].size, (1240, 1754))

    def test_header_footer_and_very_long_text(self):
        result = collage.build_collage(sources(3), header_text="W" * 120, footer_text="Footer")
        self.assertEqual(len(result.pages), 1)

    def test_pdf_is_a4_and_embeds_jpegs_untouched(self):
        result = collage.build_collage(sources(20))
        jpegs = [collage.jpeg_bytes(p) for p in result.pages]
        pdf = collage.images_to_pdf(jpegs)
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        for data in jpegs:
            self.assertIn(data, pdf)  # same bytes: no second JPEG compression
        box = re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", pdf)
        width_in, height_in = float(box.group(1)) / 72, float(box.group(2)) / 72
        self.assertAlmostEqual(width_in, 8.27, delta=0.02)
        self.assertAlmostEqual(height_in, 11.69, delta=0.02)
        self.assertEqual(pdf.count(b"/Type /Page "), len(jpegs))

    def test_pdf_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            collage.images_to_pdf([])
        with self.assertRaises(ValueError):
            collage.images_to_pdf([b"junk"])


if __name__ == "__main__":
    unittest.main()
