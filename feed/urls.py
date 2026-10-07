from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("posts/new/", views.create_post, name="create_post"),
    path("posts/<int:pk>/delete/", views.delete_post, name="delete_post"),
    path("posts/<int:pk>/like/", views.toggle_like, name="toggle_like"),
    path("posts/<int:pk>/comment/", views.add_comment, name="add_comment"),
    path("posts/<int:pk>/share/", views.share_post, name="share_post"),
]
