from django.urls import path

from . import views

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("profile/edit/", views.profile_edit, name="profile_edit"),
    path("profile/experience/add/", views.add_experience, name="add_experience"),
    path("profile/education/add/", views.add_education, name="add_education"),
    path("profile/skill/add/", views.add_skill, name="add_skill"),
    path("skills/<int:pk>/endorse/", views.endorse, name="endorse"),
]
