# Pro Collage Maker

A Django app that turns many photos into a tight, print-ready collage PDF (A4, A3 or Letter).

## Features
- Upload by clicking or drag-and-drop; drag thumbnails to change the order
- Optional header and footer text (Hindi, Bangla and other scripts work if a matching font is installed)
- Page size and spacing options
- Phone photos are rotated correctly (EXIF), transparent PNGs get a white background
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
