"""Tests for the collage engine. Run with:  python manage.py test   (or: python -m unittest)"""

import io
import re
import unittest

from PIL import Image, ImageChops

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


class PhotoTests(unittest.TestCase):
    def test_exif_rotation_swaps_dimensions(self):
        self.assertEqual(collage.Photo("a.jpg", make_jpeg(400, 300, orientation=6)).size, (300, 400))
        self.assertEqual(collage.Photo("a.jpg", make_jpeg(400, 300, orientation=1)).size, (400, 300))

    def test_unreadable_file_is_skipped_not_fatal(self):
        result = collage.build_collage([("bad.txt", io.BytesIO(b"not an image"))] + sources(1))
        self.assertEqual(result.skipped, ["bad.txt"])
        self.assertEqual(len(result.pages), 1)

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
