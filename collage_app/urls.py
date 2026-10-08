from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_and_create_collage, name='upload_collage'),
]