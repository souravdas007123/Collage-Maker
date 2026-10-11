# Pro Collage Maker

A Django app that turns many photos into a tight, print-ready collage PDF (A4, A3 or Letter).

## Features
- Upload by clicking or drag-and-drop; drag thumbnails to change the order
- Optional header and footer text (Hindi, Bangla and other scripts work if a matching font is installed)
- Page size and spacing options
- Uses the page fully: photos start at the very top and are packed down the page with an exact search, keeping
  every photo's aspect ratio (nothing is cropped or stretched). It tries to put everything on ONE page, but never by
  making photos tiny: pick a "Photo size" (Small / Normal / Large, default Normal = about 3 inch tall) and any
  photos that would have to be smaller go to the next page Space for header, footer and signature exists only when you use them
  (header text gets padding above and below it, then the photos start)
- "Turn photos 90 degrees to fill the page" (on by default) rotates photos clockwise only when that leaves
  clearly less empty space and never needs more pages; untick it to keep every photo upright
- iPhone HEIC/HEIF photos work. If the server can open them (via `pillow-heif`) they are used directly;
  otherwise (for example when Windows Application Control blocks the native library) the browser converts
  them to JPEG before upload. That needs internet access once, to load the converter from a CDN
- Signature stamp in the footer: upload an image (a photo of your signature on paper works; the paper
  background is removed) or draw it with mouse/finger. Choose left/center/right, size, and last page or
  every page. It gets its own band above the footer text, so it never covers a photo
- Live preview; the PDF contains exactly the preview images (no second JPEG compression)

## Run it
```bash
pip install -r requirements.txt
python manage.py runserver
```
Open http://127.0.0.1:8000/ . Django may print a notice about unapplied migrations; the app does not use the
database, so it can be ignored (or run `python manage.py migrate` once to silence it).

Run the tests: `python manage.py test`

## Fonts for non-English header/footer text
The engine picks the first installed font that has every character in your text (Arial/DejaVu, then Noto,
Nirmala, FreeSans ...). To force a font, either drop a `.ttf` file into `collage_app/fonts/` or set the
`COLLAGE_FONT` environment variable to a font file path. If no font fits, the page shows a warning.

## Production settings
Set these environment variables: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=0`, `DJANGO_ALLOWED_HOSTS=your.domain`.

## Layout
- `collage_app/collage.py` - collage engine (layout, rendering, PDF writer), no Django code
- `collage_app/views.py` - upload/preview/PDF endpoints with validation
- `collage_app/templates/collage_app/upload.html` - the single-page UI
