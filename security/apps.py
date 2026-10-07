from django.apps import AppConfig


class SecurityConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "security"

    def ready(self):
        from django.contrib import admin

        from . import events  # noqa: F401  (connects the sign-in signals)

        admin.site.login_form = events.LockoutAdminAuthenticationForm
