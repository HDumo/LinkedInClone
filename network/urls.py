from django.urls import path

from . import views

urlpatterns = [
    path("network/", views.my_network, name="my_network"),
    path("people/", views.people, name="people"),
    path("connect/<int:user_id>/", views.connect, name="connect"),
    path("connections/<int:pk>/respond/", views.respond, name="respond"),
    path("connections/<int:user_id>/remove/", views.remove, name="remove_connection"),
    path("search/", views.search, name="search"),
]
