from django.urls import path

from . import views

urlpatterns = [
    path("messages/", views.inbox, name="inbox"),
    path("messages/<int:user_id>/", views.thread, name="thread"),
]
