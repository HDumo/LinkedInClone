"""`manage.py check_install` — a plain-English pre-flight for a new server.

Run it after editing .env (deploy-release.sh runs it for you) to see what is
missing before visitors do. Exits non-zero if something must be fixed."""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Report configuration problems that would stop the site from working."

    def handle(self, *args, **opts):
        problems, warnings = [], []
        if settings.DEBUG:
            warnings.append("DEBUG is on. Set DEBUG=False in .env on a real server.")
        if not [h for h in settings.ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")] and not settings.DEBUG:
            problems.append("ALLOWED_HOSTS only lists localhost. Add your domain to ALLOWED_HOSTS in .env.")
        if not settings.DEBUG and not settings.CSRF_TRUSTED_ORIGINS:
            problems.append("CSRF_TRUSTED_ORIGINS is empty. Add https://your-domain to .env or logins will fail.")
        if not settings.DEBUG and not settings.HTTPS_ONLY:
            warnings.append("HTTPS_ONLY is False: logins are not protected. Finish the TLS step, then set HTTPS_ONLY=True.")
        if not settings.DEBUG and not settings.EMAIL_HOST_USER:
            warnings.append("No email account is configured (EMAIL_HOST_USER). Password-reset emails will not be delivered.")
        if "sqlite" in settings.DATABASES["default"]["ENGINE"] and not settings.DEBUG:
            warnings.append("Using SQLite. Fine for a small trial; set DATABASE_URL to PostgreSQL for real use.")
        try:
            connection.ensure_connection()
        except Exception as exc:  # noqa: BLE001 - we want to print whatever the driver says
            problems.append(f"Cannot connect to the database: {exc}")
        for w in warnings:
            self.stdout.write(self.style.WARNING(f"warning: {w}"))
        for p in problems:
            self.stdout.write(self.style.ERROR(f"problem: {p}"))
        if problems:
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Install check passed." if not warnings else "Install check passed with warnings."))
