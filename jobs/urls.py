from django.urls import path

from . import views

urlpatterns = [
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/new/", views.job_create, name="job_create"),
    path("jobs/applications/", views.my_applications, name="my_applications"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/apply/", views.job_apply, name="job_apply"),
    path("companies/", views.company_list, name="company_list"),
    path("companies/new/", views.company_create, name="company_create"),
    path("companies/<slug:slug>/", views.company_detail, name="company_detail"),
]
