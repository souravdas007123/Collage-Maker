import base64
import logging

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from . import collage

logger = logging.getLogger(__name__)

MAX_PHOTOS = 150
MAX_PHOTO_BYTES = 30 * 1024 * 1024
MAX_PDF_PAGES = 150
MAX_PAGE_BYTES = 15 * 1024 * 1024
MAX_TEXT_LENGTH = 100


def _error(message, status=400, **extra):
    return JsonResponse({"status": "error", "message": message, **extra}, status=status)


def _clean_text(value):
    """Single line, trimmed, length-limited."""
    return " ".join((value or "").split())[:MAX_TEXT_LENGTH]


def _int(value, default, low, high):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


@require_GET
def collage_page(request):
    return render(
        request,
        "collage_app/upload.html",
        {
            "page_sizes": list(collage.PAGE_SIZES),
            "default_margin": collage.DEFAULT_MARGIN,
            "max_margin": collage.MAX_MARGIN,
            "max_photos": MAX_PHOTOS,
            "max_text": MAX_TEXT_LENGTH,
        },
    )


@require_POST
def create_preview(request):
    """Build the collage pages and return them as base64 JPEGs for the preview."""
    files = request.FILES.getlist("images")
    if not files:
        return _error("Please select at least one photo.")
    if len(files) > MAX_PHOTOS:
        return _error(f"Too many photos ({len(files)}). The limit is {MAX_PHOTOS}.")

    sources, skipped = [], []
    for f in files:
        if f.size > MAX_PHOTO_BYTES:
            skipped.append(f"{f.name} (larger than {MAX_PHOTO_BYTES // (1024 * 1024)} MB)")
        else:
            sources.append((f.name, f))

    try:
        result = collage.build_collage(
            sources,
            page_size=request.POST.get("page_size", collage.DEFAULT_PAGE_SIZE),
            margin=_int(request.POST.get("margin"), collage.DEFAULT_MARGIN, 0, collage.MAX_MARGIN),
            header_text=_clean_text(request.POST.get("header_text")),
            footer_text=_clean_text(request.POST.get("footer_text")),
        )
    except Exception:
        logger.exception("Collage generation failed")
        return _error("Could not create the collage. Please try fewer or smaller photos.", status=500)

    skipped += [f"{name} ({reason})" for name, reason in zip(result.skipped, result.skipped_reasons)]
    if not result.pages:
        return _error("None of the selected files could be read as images.", skipped=skipped)

    return JsonResponse(
        {
            "status": "success",
            "pages_base64": [base64.b64encode(collage.jpeg_bytes(p)).decode("ascii") for p in result.pages],
            "skipped": skipped,
            "warnings": result.warnings,
        }
    )


@require_POST
def download_pdf(request):
    """Wrap the previewed page images in a PDF. Pages arrive as uploaded files."""
    files = request.FILES.getlist("pages")
    if not files:
        return _error("There are no pages to download. Please generate a preview first.")
    if len(files) > MAX_PDF_PAGES:
        return _error(f"Too many pages ({len(files)}). The limit is {MAX_PDF_PAGES}.")
    if any(f.size > MAX_PAGE_BYTES for f in files):
        return _error("One of the pages is too large.")

    try:
        pdf = collage.images_to_pdf([f.read() for f in files])
    except ValueError as exc:
        return _error(str(exc))
    except Exception:
        logger.exception("PDF generation failed")
        return _error("Could not create the PDF.", status=500)

    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="my_pro_collage.pdf"'
    return response
