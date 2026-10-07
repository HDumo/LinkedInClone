import io
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from accounts.tests import make_user
from core.ip import client_ip
from core.models import RateCounter
from core.ratecache import DatabaseCounterCache, prune

BASE = Path(__file__).resolve().parent.parent


class HealthAndStaticPages(TestCase):
    def test_healthz_ok_without_login(self):
        r = self.client.get(reverse("healthz"))
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))

    def test_healthz_503_when_database_down(self):
        with mock.patch("core.views.connection.cursor", side_effect=Exception("db down")):
            self.assertEqual(self.client.get(reverse("healthz")).status_code, 503)

    def test_robots_txt(self):
        r = self.client.get("/robots.txt")
        self.assertContains(r, "Disallow: /admin/")
        self.assertEqual(r["Content-Type"].split(";")[0], "text/plain")

    def test_security_headers_present(self):
        r = self.client.get(reverse("login"))
        self.assertIn("script-src 'none'", r["Content-Security-Policy"])
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_login_page_has_favicon_and_reset_link(self):
        r = self.client.get(reverse("login"))
        self.assertContains(r, "favicon.svg")
        self.assertContains(r, reverse("password_reset"))


@override_settings(DEBUG=False)
class ErrorPages(TestCase):
    def test_404_uses_friendly_page(self):
        r = self.client.get("/definitely/not/here/")
        self.assertContains(r, "Page not found", status_code=404)

    def test_500_page_renders(self):
        from core.views import server_error

        r = server_error(RequestFactory().get("/"))
        self.assertEqual(r.status_code, 500)
        self.assertIn(b"Something went wrong", r.content)

    def test_csrf_failure_page(self):
        c = self.client_class(enforce_csrf_checks=True)
        r = c.post(reverse("login"), {"username": "a", "password": "b"})
        self.assertContains(r, "Session expired", status_code=403)


class ClientIp(TestCase):
    def test_uses_last_forwarded_hop_so_spoofing_fails(self):
        req = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.9", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(client_ip(req), "203.0.113.9")

    def test_garbage_header_falls_back_to_socket(self):
        req = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="not-an-ip", REMOTE_ADDR="10.1.2.3")
        self.assertEqual(client_ip(req), "10.1.2.3")

    def test_no_header(self):
        self.assertEqual(client_ip(RequestFactory().get("/", REMOTE_ADDR="10.1.2.3")), "10.1.2.3")


class RateCounterCache(TestCase):
    def setUp(self):
        self.cache = DatabaseCounterCache("x", {})

    def test_add_incr_get(self):
        self.assertTrue(self.cache.add("k", 1, 60))
        self.assertFalse(self.cache.add("k", 1, 60))
        self.assertEqual(self.cache.incr("k"), 2)
        self.assertEqual(self.cache.get("k"), 2)

    def test_incr_missing_raises(self):
        with self.assertRaises(ValueError):
            self.cache.incr("nope")

    def test_expired_counter_is_ignored_and_pruned(self):
        self.cache.add("old", 5, 0)
        self.assertIsNone(self.cache.get("old"))
        self.assertEqual(prune(), 1)
        self.assertEqual(RateCounter.objects.count(), 0)


class RateLimiting(TestCase):
    def post_login(self, ip, user="nobody", pw="wrong"):
        return self.client.post(reverse("login"), {"username": user, "password": pw}, HTTP_X_FORWARDED_FOR=ip)

    def test_eleventh_login_attempt_per_minute_blocked(self):
        codes = [self.post_login("203.0.113.5").status_code for _ in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 403)

    def test_limit_is_per_ip_and_spoofed_header_does_not_help(self):
        for _ in range(11):
            self.post_login("203.0.113.5")
        # a different real client is unaffected
        self.assertEqual(self.post_login("203.0.113.77").status_code, 200)
        # prepending a fake address does not reset the budget (last hop wins)
        self.assertEqual(self.post_login("1.2.3.4, 203.0.113.5").status_code, 403)

    def test_correct_password_still_works_under_limit(self):
        make_user("alice")
        r = self.post_login("203.0.113.9", "alice", "pw-12345-xyz")
        self.assertEqual(r.status_code, 302)

    def test_signup_limited_to_ten_per_hour(self):
        codes = [self.client.post(reverse("signup"), {}, HTTP_X_FORWARDED_FOR="203.0.113.40").status_code for _ in range(11)]
        self.assertEqual(codes[-1], 403)
        self.assertEqual(set(codes[:-1]), {200})


class PasswordFlows(TestCase):
    def setUp(self):
        self.user = make_user("alice")

    def test_reset_email_sent_and_link_works(self):
        r = self.client.post(reverse("password_reset"), {"email": "alice@example.com"})
        self.assertRedirects(r, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"https?://\S+/accounts/reset/\S+/", mail.outbox[0].body).group(0)
        path = link.split("testserver")[-1]
        r = self.client.get(path, follow=True)
        self.assertEqual(r.status_code, 200)
        r = self.client.post(r.request["PATH_INFO"], {"new_password1": "a-new-strong-pass-77", "new_password2": "a-new-strong-pass-77"})
        self.assertRedirects(r, reverse("password_reset_complete"))
        self.assertTrue(self.client.login(username="alice", password="a-new-strong-pass-77"))

    def test_reset_for_unknown_email_reveals_nothing(self):
        r = self.client.post(reverse("password_reset"), {"email": "ghost@example.com"})
        self.assertRedirects(r, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_bad_reset_link_shows_message(self):
        r = self.client.get("/accounts/reset/zz/set-password/", follow=True)
        self.assertContains(r, "invalid or has already been used")

    def test_change_password_requires_login_and_works(self):
        self.assertEqual(self.client.get(reverse("password_change")).status_code, 302)
        self.client.force_login(self.user)
        r = self.client.post(reverse("password_change"), {
            "old_password": "pw-12345-xyz", "new_password1": "another-strong-pass-1", "new_password2": "another-strong-pass-1"})
        self.assertRedirects(r, reverse("password_change_done"))

    def test_weak_password_rejected(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("password_change"), {
            "old_password": "pw-12345-xyz", "new_password1": "short1", "new_password2": "short1"})
        self.assertEqual(r.status_code, 200)


class PhotoUpload(TestCase):
    def test_oversized_photo_rejected(self):
        u = make_user("alice")
        self.client.force_login(u)
        with override_settings(MAX_PHOTO_BYTES=10):
            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (40, 40), "red").save(buf, "PNG")
            f = SimpleUploadedFile("a.png", buf.getvalue(), content_type="image/png")
            r = self.client.post(reverse("profile_edit"), {"headline": "x", "photo": f})
        self.assertContains(r, "5 MB or smaller")

    def test_non_image_rejected(self):
        u = make_user("alice")
        self.client.force_login(u)
        f = SimpleUploadedFile("a.png", b"not an image", content_type="image/png")
        r = self.client.post(reverse("profile_edit"), {"headline": "x", "photo": f})
        self.assertEqual(r.status_code, 200)
        u.profile.refresh_from_db()
        self.assertFalse(u.profile.photo)


class CheckInstallCommand(TestCase):
    def run_cmd(self, **overrides):
        out = io.StringIO()
        with override_settings(**overrides):
            call_command("check_install", stdout=out)
        return out.getvalue()

    def test_passes_in_dev(self):
        self.assertIn("Install check passed", self.run_cmd(DEBUG=True))

    def test_flags_missing_domain_and_csrf_origin(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(DEBUG=False, ALLOWED_HOSTS=["localhost"], CSRF_TRUSTED_ORIGINS=[])

    def test_warns_when_https_only_is_off(self):
        out = self.run_cmd(DEBUG=False, HTTPS_ONLY=False, ALLOWED_HOSTS=["example.com"], CSRF_TRUSTED_ORIGINS=["http://example.com"])
        self.assertIn("HTTPS_ONLY is False", out)

    def test_prod_with_domain_passes_with_email_warning(self):
        out = self.run_cmd(DEBUG=False, ALLOWED_HOSTS=["example.com"], CSRF_TRUSTED_ORIGINS=["https://example.com"], EMAIL_HOST_USER="")
        self.assertIn("passed with warnings", out)


class ProductionSettings(TestCase):
    """Boot Django exactly as the server does (DEBUG off) and run its own deploy checklist."""

    def run_manage(self, *args, **env):
        # Hermetic: ignore this machine's real .env and any config in the environment.
        keep = ("PATH", "HOME", "LANG", "PYTHONPATH", "VIRTUAL_ENV")
        e = {k: v for k, v in os.environ.items() if k in keep}
        e["ENV_FILE"] = os.devnull
        e.update(env)
        return subprocess.run([sys.executable, "manage.py", *args], cwd=BASE, env=e, capture_output=True, text=True)

    def prod_env(self, **extra):
        env = dict(
            DEBUG="False", DJANGO_SECRET_KEY="k3Jx9vQ2mZpL7sWc4RtYbN8fHaGdE5uXoI1yTqVr6Mn0CwBjAeUz", ALLOWED_HOSTS="example.com",
            CSRF_TRUSTED_ORIGINS="https://example.com", DATABASE_URL="",
        )
        env.update(extra)
        return env

    def test_refuses_to_start_without_secret_key(self):
        r = self.run_manage("check", DEBUG="False")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", r.stderr)

    def test_https_only_off_allows_http_logins_before_certificate(self):
        r = self.run_manage("shell", "-c", "from django.conf import settings as s; print(s.SESSION_COOKIE_SECURE, s.SECURE_SSL_REDIRECT)",
                            **self.prod_env(HTTPS_ONLY="False"))
        self.assertEqual(r.stdout.strip().splitlines()[-1], "False False", r.stderr)

    def test_sqlite_path_env_is_honoured(self):
        r = self.run_manage("shell", "-c", "from django.conf import settings as s; print(s.DATABASES['default']['NAME'])",
                            **self.prod_env(SQLITE_PATH="/tmp/x/shared.sqlite3"))
        self.assertEqual(r.stdout.strip().splitlines()[-1], "/tmp/x/shared.sqlite3", r.stderr)

    def test_check_deploy_has_no_warnings(self):
        r = self.run_manage("check", "--deploy", "--fail-level", "WARNING", **self.prod_env())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class DeployFiles(TestCase):
    """The server tooling can't be fully run in CI, so guard what can be checked."""

    DEPLOY = BASE / "deploy"

    def test_shell_scripts_are_valid_bash_and_executable(self):
        for name in ("deploy-release.sh", "provision.sh"):
            path = self.DEPLOY / name
            self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")
            r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_works_without_root_or_a_server(self):
        for name in ("deploy-release.sh", "provision.sh"):
            r = subprocess.run([str(self.DEPLOY / name), "--help"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("USAGE" if name.startswith("deploy") else "Options", r.stdout)

    def test_unknown_command_is_rejected(self):
        r = subprocess.run([str(self.DEPLOY / "deploy-release.sh"), "frobnicate"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Unknown command", r.stdout + r.stderr)

    def test_templates_use_only_placeholders_provision_fills_in(self):
        known = {"__APP_ROOT__", "__DOMAIN__", "__SERVER_NAMES__"}
        for f in self.DEPLOY.iterdir():
            if f.suffix in (".conf", ".service", ".timer") and f.name != "deploy.conf":
                found = set(re.findall(r"__[A-Z_]+__", f.read_text()))
                self.assertLessEqual(found, known, f"{f.name} has unknown placeholders {found - known}")

    def test_service_unit_points_at_the_current_symlink_and_real_wsgi_module(self):
        unit = (self.DEPLOY / "linkedclone.service").read_text()
        self.assertIn("__APP_ROOT__/current/venv/bin/gunicorn", unit)
        self.assertIn("config.wsgi:application", unit)
        # gunicorn 26 otherwise tries to create a control socket in a folder the service user can't write
        self.assertIn("--no-control-socket", unit)
        self.assertRegex((BASE / "requirements.txt").read_text(), r"gunicorn>=26\.\d+")
        self.assertTrue((BASE / "config" / "wsgi.py").exists())

    def test_nginx_serves_media_from_shared_and_static_through_current(self):
        for name in ("nginx.conf", "nginx-http.conf"):
            text = (self.DEPLOY / name).read_text()
            self.assertIn("alias __APP_ROOT__/current/staticfiles/;", text)
            self.assertIn("alias __APP_ROOT__/shared/media/;", text)
            self.assertIn("X-Forwarded-For $proxy_add_x_forwarded_for", text)  # core.ip.client_ip relies on this

    def test_env_example_documents_every_setting_the_provisioner_writes(self):
        text = (BASE / ".env.example").read_text()
        for key in ("DEBUG", "DJANGO_SECRET_KEY", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "HTTPS_ONLY",
                    "DATABASE_URL", "SQLITE_PATH", "DEFAULT_FROM_EMAIL"):
            self.assertRegex(text, rf"(?m)^{key}=", key)
