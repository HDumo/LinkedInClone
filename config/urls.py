from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from accounts import views as account_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/", include("accounts.urls")),
    path("in/<str:username>/", account_views.profile_detail, name="profile"),
    path("", include("feed.urls")),
    path("", include("network.urls")),
    path("", include("messaging.urls")),
    path("", include("jobs.urls")),
    path("", include("notifications.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
