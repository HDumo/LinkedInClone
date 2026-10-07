from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from django_ratelimit.decorators import ratelimit

from accounts import views as account_views
from core import views as core_views

handler400 = "core.views.bad_request"
handler403 = "core.views.permission_denied"
handler404 = "core.views.not_found"
handler500 = "core.views.server_error"


def limited(view, rate):
    """Throttle POSTs (password guesses, reset emails) per client IP."""
    return ratelimit(key="ip", rate=rate, method="POST", block=True)(view)


urlpatterns = [
    path("healthz/", core_views.healthz, name="healthz"),
    path("robots.txt", core_views.robots_txt, name="robots"),
    path("admin/", admin.site.urls),
    path("accounts/login/", limited(auth_views.LoginView.as_view(), "10/m"), name="login"),
    path("accounts/password_reset/", limited(auth_views.PasswordResetView.as_view(), "5/h"), name="password_reset"),
    path("accounts/", include("django.contrib.auth.urls")),
    path("accounts/", include("accounts.urls")),
    path("in/<str:username>/", account_views.profile_detail, name="profile"),
    path("", include("feed.urls")),
    path("", include("network.urls")),
    path("", include("messaging.urls")),
    path("", include("jobs.urls")),
    path("", include("notifications.urls")),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
