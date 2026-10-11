from django.urls import path

from . import views

urlpatterns = [
    path("", views.collage_page, name="upload_collage"),
    path("preview/", views.create_preview, name="create_preview"),
    path("pdf/", views.download_pdf, name="download_pdf"),
]
