import re
import time
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.tests import make_user
from feed.models import Comment, Like, Post
from notifications.models import Notification

from . import iplocate, rules, throttle
from .events import PasswordReuseValidator
from .models import (AccountSecurity, LoginEvent, LoginLockout, PageView, PasswordHistory, SecurityConfig,
                     SecurityRule, UnlockCode, UserSession, VisitorIP)

PW = "pw-12345-xyz"
UA_CHROME_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"
UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit Mobile Safari"


def login_post(client, username, password, ip="45.33.32.10", ua=UA_CHROME_WIN, **extra):
    return client.post(reverse("login"), {"username": username, "password": password},
                       HTTP_X_FORWARDED_FOR=ip, HTTP_USER_AGENT=ua, **extra)


def make_staff(name="staffer"):
    return make_user(name, is_staff=True)


def make_admin(name="boss"):
    return make_user(name, is_staff=True, is_superuser=True)


# --------------------------------------------------------------------------- login history
class LoginHistoryTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_successful_login_is_recorded_with_device_and_ip(self):
        r = login_post(self.client, "alice", PW, ip="45.33.33.7")
        self.assertEqual(r.status_code, 302)
        e = LoginEvent.objects.get()
        self.assertEqual((e.result, e.user, e.username, e.ip_address, e.page), (LoginEvent.OK, self.alice, "alice", "45.33.33.7", "member"))
        self.assertEqual(e.device, "Chrome on Windows")

    def test_failures_are_classified(self):
        login_post(self.client, "alice", "wrong-pass")
        login_post(self.client, "nobody", "whatever", ip="45.33.32.11")
        self.alice.is_active = False
        self.alice.save()
        login_post(self.client, "alice", PW, ip="45.33.32.12")
        got = set(LoginEvent.objects.values_list("result", flat=True))
        self.assertEqual(got, {LoginEvent.BAD_PASSWORD, LoginEvent.UNKNOWN_USER, LoginEvent.INACTIVE})

    def test_password_is_never_stored_in_history(self):
        login_post(self.client, "alice", "super-secret-attempt-1")
        blob = " ".join(str(v) for e in LoginEvent.objects.all() for v in (e.username, e.result, e.device, e.location))
        self.assertNotIn("super-secret-attempt-1", blob)

    def test_new_device_gets_a_notice_but_first_login_does_not(self):
        login_post(self.client, "alice", PW, ua=UA_CHROME_WIN)
        self.assertEqual(Notification.objects.filter(recipient=self.alice, system=True).count(), 0)
        login_post(Client(), "alice", PW, ua=UA_IPHONE, ip="45.33.33.99")
        note = Notification.objects.get(recipient=self.alice, system=True)
        self.assertIn("New sign-in from Safari on iPhone", note.verb)
        # same device and place again: no new notice
        login_post(Client(), "alice", PW, ua=UA_IPHONE, ip="45.33.33.99")
        self.assertEqual(Notification.objects.filter(recipient=self.alice, system=True).count(), 1)

    def test_system_notice_renders_without_a_fake_actor_name(self):
        login_post(self.client, "alice", PW, ua=UA_CHROME_WIN)
        login_post(self.client, "alice", PW, ua=UA_IPHONE, ip="45.33.33.99")
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse("notifications")), "New sign-in from")

    def test_failed_staff_login_alerts_administrators(self):
        staff, boss = make_staff(), make_admin()
        login_post(self.client, "staffer", "wrong")
        self.assertTrue(Notification.objects.filter(recipient=boss, system=True, verb__contains="staffer").exists())
        self.assertFalse(Notification.objects.filter(recipient=staff, system=True).exists())

    def test_my_activity_shows_only_my_events(self):
        make_user("bob")
        login_post(Client(), "bob", "wrong-pass", ip="45.33.32.50")
        login_post(self.client, "alice", PW)
        r = self.client.get(reverse("my_activity"))
        self.assertContains(r, "Signed in")
        self.assertNotContains(r, "45.33.32.50")

    def test_logging_failure_never_blocks_sign_in(self):
        with mock.patch("security.models.LoginEvent.objects.create", side_effect=Exception("db")):
            self.assertEqual(login_post(self.client, "alice", PW).status_code, 302)


# --------------------------------------------------------------------------- lockout
class LockoutTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice", email="alice@example.com")

    def fail(self, n, user="alice", ip="45.33.32.20"):
        last = None
        for _ in range(n):
            last = login_post(self.client, user, "wrong-pass", ip=ip)
        return last

    def test_escalating_warnings_then_lock(self):
        self.assertNotContains(self.fail(1), "remaining")
        self.assertContains(self.fail(1), "2 attempts remaining")
        self.assertContains(self.fail(1), "1 attempt remaining")
        r = self.fail(1)
        self.assertContains(r, "Too many failed attempts")
        self.assertTrue(LoginLockout.objects.get(username="alice").is_locked)

    def test_correct_password_is_refused_while_locked_and_logged(self):
        self.fail(4)
        r = login_post(self.client, "alice", PW, ip="45.33.32.20")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Too many failed attempts")
        self.assertTrue(LoginEvent.objects.filter(result=LoginEvent.LOCKED).exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_usernames_lock_exactly_like_real_ones(self):
        """Otherwise the lockout itself would reveal which usernames exist."""
        real = self.fail(4, "alice", ip="45.33.32.21").content.decode()
        fake = self.fail(4, "no-such-person", ip="45.33.32.22").content.decode()
        for page in (real, fake):
            self.assertIn("Too many failed attempts", page)
        self.assertTrue(LoginLockout.objects.get(username="no-such-person").is_locked)
        self.assertIsNone(LoginLockout.objects.get(username="no-such-person").user)

    def test_lock_expires_and_success_resets_counter(self):
        self.fail(4)
        LoginLockout.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(login_post(self.client, "alice", PW, ip="45.33.32.20").status_code, 302)
        self.assertEqual(LoginLockout.objects.get(username="alice").failed_attempts, 0)

    def test_lock_wait_grows(self):
        self.fail(4)
        first = LoginLockout.objects.get(username="alice").locked_until
        LoginLockout.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        self.fail(1)
        second = LoginLockout.objects.get(username="alice").locked_until
        self.assertGreater(second - timezone.now(), timedelta(minutes=1, seconds=30))
        self.assertGreater(second, first)

    def test_lock_is_per_account_not_per_ip(self):
        self.fail(4, ip="45.33.32.30")
        make_user("bob")
        self.assertEqual(login_post(Client(), "bob", PW, ip="45.33.32.30").status_code, 302)

    def test_admin_login_page_has_the_same_lockout(self):
        make_admin("boss")
        for _ in range(4):
            self.client.post("/admin/login/?next=/admin/", {"username": "boss", "password": "bad", "next": "/admin/"},
                             HTTP_X_FORWARDED_FOR="45.33.32.40")
        r = self.client.post("/admin/login/?next=/admin/", {"username": "boss", "password": PW, "next": "/admin/"},
                             HTTP_X_FORWARDED_FOR="45.33.32.40")
        self.assertContains(r, "Too many failed attempts")
        self.assertEqual(LoginEvent.objects.filter(page="admin").count(), 5)

    def test_staff_can_unlock_from_portal(self):
        self.fail(4)
        self.client.force_login(make_staff())
        lock = LoginLockout.objects.get(username="alice")
        self.client.post(reverse("security_locked"), {"lockout_id": lock.id})
        lock.refresh_from_db()
        self.assertFalse(lock.is_locked)
        self.assertEqual(lock.failed_attempts, 0)

    def test_password_reset_clears_the_lock(self):
        self.fail(4)
        validator = PasswordReuseValidator()
        validator.password_changed("x", self.alice)
        self.assertFalse(LoginLockout.objects.get(username="alice").is_locked)


# --------------------------------------------------------------------------- unlock with a code
class UnlockCodeTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice", email="alice@example.com")
        for _ in range(4):
            login_post(self.client, "alice", "bad", ip="45.33.32.60")
        self.c = Client()

    def request_code(self, ident="alice", ip="45.33.33.1"):
        return self.c.post(reverse("unlock_request"), {"identifier": ident}, HTTP_X_FORWARDED_FOR=ip, follow=True)

    def code(self):
        return re.search(r"unlock code is (\d{6})", mail.outbox[-1].body).group(1)

    def test_full_flow_unlocks_and_logs(self):
        self.request_code()
        r = self.c.post(reverse("unlock_confirm"), {"code": self.code()}, HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True)
        self.assertContains(r, "Your account is unlocked")
        self.assertFalse(LoginLockout.objects.get(username="alice").is_locked)
        self.assertTrue(LoginEvent.objects.filter(result=LoginEvent.OTP_UNLOCK, user=self.alice).exists())
        self.assertEqual(login_post(Client(), "alice", PW, ip="45.33.32.60").status_code, 302)

    def test_works_with_email_address_too(self):
        self.request_code("alice@example.com")
        self.assertEqual(len(mail.outbox), 1)

    def test_unknown_account_gets_the_same_answer_and_no_email(self):
        known = self.request_code("alice").content.decode()
        mail.outbox.clear()
        unknown = Client().post(reverse("unlock_request"), {"identifier": "ghost"}, HTTP_X_FORWARDED_FOR="45.33.33.2", follow=True).content.decode()
        self.assertEqual(len(mail.outbox), 0)
        strip = lambda s: re.sub(r"csrfmiddlewaretoken\" value=\"[^\"]+\"|alice|ghost", "", s)
        self.assertEqual(strip(known), strip(unknown))

    def test_wrong_code_is_rejected_and_counted(self):
        self.request_code()
        r = self.c.post(reverse("unlock_confirm"), {"code": "000000" if self.code() != "000000" else "111111"},
                        HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True)
        self.assertContains(r, "Check it and try again")
        self.assertTrue(LoginLockout.objects.get(username="alice").is_locked)
        self.assertEqual(UnlockCode.objects.get().attempts, 1)

    def test_expired_code_fails(self):
        self.request_code()
        code = self.code()
        UnlockCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        r = self.c.post(reverse("unlock_confirm"), {"code": code}, HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True)
        self.assertContains(r, "Check it and try again")

    def test_code_is_single_use_and_newer_code_replaces_older(self):
        self.request_code()
        old = self.code()
        self.request_code()
        new = self.code()
        self.assertEqual(UnlockCode.objects.filter(used_at__isnull=True).count(), 1)
        if old != new:
            self.assertContains(self.c.post(reverse("unlock_confirm"), {"code": old}, HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True), "Check it and try again")
        self.c.post(reverse("unlock_confirm"), {"code": new}, HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True)
        self.assertFalse(LoginLockout.objects.get(username="alice").is_locked)
        self.assertEqual(UnlockCode.objects.filter(used_at__isnull=True).count(), 0)
        # the used code can't unlock again
        LoginLockout.objects.update(failed_attempts=4, locked_until=timezone.now() + timedelta(minutes=5))
        session = self.c.session
        session["unlock_identifier"] = "alice"
        session.save()
        r = self.c.post(reverse("unlock_confirm"), {"code": new}, HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True)
        self.assertContains(r, "Check it and try again")
        self.assertTrue(LoginLockout.objects.get(username="alice").is_locked)

    def test_codes_are_stored_hashed(self):
        self.request_code()
        self.assertNotIn(self.code(), UnlockCode.objects.get().code_hash)

    def test_attempts_per_code_are_limited(self):
        self.request_code()
        code = self.code()
        wrong = "123456" if code != "123456" else "654321"
        entry = UnlockCode.objects.get()
        entry.attempts = 5
        entry.save()
        r = self.c.post(reverse("unlock_confirm"), {"code": code}, HTTP_X_FORWARDED_FOR="45.33.33.1", follow=True)
        self.assertContains(r, "Check it and try again")  # right code, but already out of tries
        self.assertNotEqual(wrong, code)

    def test_asking_for_codes_is_limited_per_address(self):
        for _ in range(4):
            self.request_code(ip="45.33.33.9")
        r = self.request_code(ip="45.33.33.9")
        self.assertContains(r, "Too many tries")

    def test_guessing_codes_is_throttled(self):
        self.request_code(ip="45.33.33.10")
        for _ in range(4):
            self.c.post(reverse("unlock_confirm"), {"code": "000001"}, HTTP_X_FORWARDED_FOR="45.33.33.10")
        r = self.c.post(reverse("unlock_confirm"), {"code": self.code()}, HTTP_X_FORWARDED_FOR="45.33.33.10", follow=True)
        self.assertContains(r, "Too many tries")
        self.assertTrue(LoginLockout.objects.get(username="alice").is_locked)

    def test_only_three_codes_per_15_minutes(self):
        for i in range(5):
            self.request_code(ip=f"45.33.33.{20 + i}")
        self.assertEqual(len(mail.outbox), 3)

    def test_confirm_without_request_redirects(self):
        self.assertRedirects(Client().get(reverse("unlock_confirm")), reverse("unlock_request"))


class ThrottleUnit(TestCase):
    def test_free_tries_then_growing_waits_then_clear(self):
        waits = [throttle.record_try("k") for _ in range(7)]
        self.assertEqual(waits, [0, 0, 0, 60, 120, 180, 300])
        self.assertGreater(throttle.wait_seconds("k"), 0)
        throttle.clear("k")
        self.assertEqual(throttle.wait_seconds("k"), 0)

    def test_keys_are_case_insensitive_and_independent(self):
        for _ in range(4):
            throttle.record_try("Alice")
        self.assertGreater(throttle.wait_seconds("alice"), 0)
        self.assertEqual(throttle.wait_seconds("bob"), 0)


# --------------------------------------------------------------------------- password rules
class PasswordRuleTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        self.client.force_login(self.alice)

    def change(self, old, new):
        return self.client.post(reverse("password_change"), {"old_password": old, "new_password1": new, "new_password2": new})

    def test_cannot_reuse_current_password_by_default(self):
        r = self.change(PW, PW)
        self.assertContains(r, "current password")

    def test_block_five_recent(self):
        cfg = SecurityConfig.get()
        cfg.password_reuse_block = SecurityConfig.REUSE_FIVE
        cfg.save()
        p1, p2, p3 = "first-new-pass-111", "second-new-pass-222", "third-new-pass-333"
        self.assertEqual(self.change(PW, p1).status_code, 302)
        self.assertEqual(self.change(p1, p2).status_code, 302)
        self.assertEqual(self.change(p2, p3).status_code, 302)
        self.assertContains(self.change(p3, p1), "recently")
        self.assertContains(self.change(p3, p2), "recently")

    def test_reuse_allowed_when_switched_off(self):
        cfg = SecurityConfig.get()
        cfg.password_reuse_block = SecurityConfig.REUSE_ALLOW
        cfg.save()
        self.assertEqual(self.change(PW, PW).status_code, 302)

    def test_history_keeps_only_hashes_and_only_five(self):
        for i in range(8):
            old = PW if i == 0 else f"loop-password-{i - 1}-abc"
            self.change(old, f"loop-password-{i}-abc")
        self.assertEqual(PasswordHistory.objects.filter(user=self.alice).count(), 5)
        self.assertFalse(any("loop-password" in h for h in PasswordHistory.objects.values_list("password", flat=True)))
        self.assertIsNotNone(AccountSecurity.for_user(self.alice).password_changed_at)

    def test_min_length_ten_enforced(self):
        self.assertContains(self.change(PW, "short-1"), "at least 10")

    def test_passwords_use_argon2_outside_tests(self):
        from django.conf import settings
        self.assertTrue(settings.PASSWORD_HASHERS[0].endswith("MD5PasswordHasher") or "Argon2" in settings.PASSWORD_HASHERS[0])
        import subprocess, sys, os
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME")}
        env.update(ENV_FILE=os.devnull, DEBUG="True")
        out = subprocess.run([sys.executable, "manage.py", "shell", "-c",
                              "from django.contrib.auth.hashers import make_password; print(make_password('x').split('$')[0])"],
                             capture_output=True, text=True, env=env, cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        self.assertEqual(out.stdout.strip().splitlines()[-1], "argon2", out.stderr)


class MustChangePasswordTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")
        AccountSecurity.objects.create(user=self.alice, must_change_password=True)
        self.client.force_login(self.alice)

    def test_everything_redirects_to_change_page(self):
        for name in ("home", "job_list", "inbox", "notifications"):
            r = self.client.get(reverse(name))
            self.assertRedirects(r, "/accounts/password_change/?required=1", fetch_redirect_response=False)

    def test_change_page_logout_static_and_health_stay_reachable(self):
        self.assertContains(self.client.get("/accounts/password_change/?required=1"), "requires you to choose a new password")
        self.assertEqual(self.client.get("/healthz/").status_code, 200)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)

    def test_changing_the_password_lifts_the_requirement(self):
        r = self.client.post(reverse("password_change"), {"old_password": PW, "new_password1": "brand-new-pass-77", "new_password2": "brand-new-pass-77"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_staff_can_require_and_clear_it(self):
        bob = make_user("bob")
        self.client.force_login(make_staff())
        self.client.post(reverse("security_people"), {"user_id": bob.id, "action": "require_change"})
        self.assertTrue(AccountSecurity.objects.get(user=bob).must_change_password)
        self.client.post(reverse("security_people"), {"user_id": bob.id, "action": "clear_require"})
        self.assertFalse(AccountSecurity.objects.get(user=bob).must_change_password)


# --------------------------------------------------------------------------- devices and sessions
class DeviceTests(TestCase):
    def setUp(self):
        self.alice = make_user("alice")

    def test_sessions_are_recorded_and_listed(self):
        a, b = Client(), Client()
        login_post(a, "alice", PW, ua=UA_CHROME_WIN)
        login_post(b, "alice", PW, ua=UA_IPHONE, ip="45.33.33.5")
        self.assertEqual(UserSession.objects.filter(user=self.alice).count(), 2)
        page = a.get(reverse("signed_in_devices"))
        self.assertContains(page, "Safari on iPhone")
        self.assertContains(page, "This device")

    def test_sign_out_other_devices(self):
        a, b = Client(), Client()
        login_post(a, "alice", PW)
        login_post(b, "alice", PW, ua=UA_IPHONE, ip="45.33.33.5")
        a.post(reverse("signed_in_devices"), {"action": "sign_out_others"})
        self.assertEqual(b.get(reverse("home")).status_code, 302)  # b is signed out
        self.assertEqual(a.get(reverse("home")).status_code, 200)  # a stays in

    def test_sign_out_one_device_only_affects_my_devices(self):
        make_user("bob")
        a, b, c = Client(), Client(), Client()
        login_post(a, "alice", PW)
        login_post(b, "alice", PW, ua=UA_IPHONE, ip="45.33.33.5")
        login_post(c, "bob", PW, ip="45.33.33.6")
        bobs = UserSession.objects.get(user__username="bob")
        a.post(reverse("signed_in_devices"), {"action": "sign_out_one", "device_id": bobs.id})
        self.assertEqual(c.get(reverse("home")).status_code, 200)  # not alice's device: untouched
        mine = UserSession.objects.get(user=self.alice, device__contains="iPhone")
        a.post(reverse("signed_in_devices"), {"action": "sign_out_one", "device_id": mine.id})
        self.assertEqual(b.get(reverse("home")).status_code, 302)

    def test_logout_removes_the_device_row(self):
        a = Client()
        login_post(a, "alice", PW)
        a.post(reverse("logout"))
        self.assertEqual(UserSession.objects.count(), 0)

    def test_password_reset_signs_out_everywhere(self):
        a = Client()
        login_post(a, "alice", PW)
        r = Client().post(reverse("password_reset"), {"email": "alice@example.com"}, HTTP_X_FORWARDED_FOR="45.33.33.77")
        self.assertEqual(len(mail.outbox), 1)
        path = re.search(r"https?://\S+(/accounts/reset/\S+/)", mail.outbox[0].body).group(1)
        c = Client()
        landing = c.get(path, follow=True)
        c.post(landing.request["PATH_INFO"], {"new_password1": "after-reset-pass-91", "new_password2": "after-reset-pass-91"})
        self.assertEqual(a.get(reverse("home")).status_code, 302)
        self.assertEqual(login_post(Client(), "alice", "after-reset-pass-91", ip="45.33.33.78").status_code, 302)

    def test_device_labels(self):
        from .devices import device_label, device_type
        self.assertEqual(device_label(UA_IPHONE), "Safari on iPhone")
        self.assertEqual(device_label(""), "Browser on an unknown device")
        self.assertEqual(device_type(UA_IPHONE), "phone")
        self.assertEqual(device_type(UA_CHROME_WIN), "computer")


# --------------------------------------------------------------------------- visitors and blocking
class VisitorBlockingTests(TestCase):
    IP = "45.33.32.77"

    def get(self, path="/accounts/login/", ip=None, **kw):
        return self.client.get(path, HTTP_X_FORWARDED_FOR=ip or self.IP, **kw)

    def test_visits_are_counted_per_address(self):
        for _ in range(3):
            self.get()
        self.get(ip="45.33.33.1")
        self.assertEqual(VisitorIP.objects.get(ip_address=self.IP).hit_count, 3)
        self.assertEqual(VisitorIP.objects.count(), 2)

    def test_static_admin_health_and_security_pages_are_not_counted(self):
        for p in ("/healthz/", "/robots.txt", "/static/favicon.svg", "/admin/login/"):
            self.get(p)
        self.assertEqual(VisitorIP.objects.filter(hit_count__gt=0).count(), 0)

    def test_blocked_address_is_refused_everywhere_except_admin_and_health(self):
        VisitorIP.objects.create(ip_address=self.IP, blocked=True)
        self.assertEqual(self.get().status_code, 403)
        self.assertEqual(self.client.post(reverse("login"), {}, HTTP_X_FORWARDED_FOR=self.IP).status_code, 403)
        self.assertEqual(self.get("/healthz/").status_code, 200)
        self.assertEqual(self.get("/admin/login/").status_code, 200)

    def test_other_addresses_unaffected_and_spoofed_header_cannot_dodge(self):
        VisitorIP.objects.create(ip_address=self.IP, blocked=True)
        self.assertEqual(self.get(ip="45.33.33.2").status_code, 200)
        # an attacker claiming to be someone else still arrives with their own last hop
        self.assertEqual(self.get(ip=f"45.33.33.2, {self.IP}").status_code, 403)

    def test_documentation_ranges_count_as_private_too(self):
        VisitorIP.objects.create(ip_address="203.0.113.5", blocked=True)
        self.assertEqual(self.get(ip="203.0.113.5").status_code, 200)

    def test_loopback_and_private_addresses_are_never_blocked(self):
        VisitorIP.objects.create(ip_address="127.0.0.1", blocked=True)
        VisitorIP.objects.create(ip_address="10.1.2.3", blocked=True)
        self.assertEqual(self.client.get("/accounts/login/").status_code, 200)
        self.assertEqual(self.get(ip="10.1.2.3").status_code, 200)

    def test_whitelist_beats_a_block(self):
        VisitorIP.objects.create(ip_address=self.IP, blocked=True)
        cfg = SecurityConfig.get()
        cfg.whitelist = "45.33.32.0/24  # office"
        cfg.save()
        self.assertEqual(self.get().status_code, 200)

    def test_a_database_error_does_not_lock_everyone_out(self):
        with mock.patch("security.models.VisitorIP.objects") as objs:
            objs.filter.side_effect = Exception("db down")
            objs.get_or_create.side_effect = Exception("db down")
            self.assertEqual(self.get().status_code, 200)


class SecurityRuleTests(TestCase):
    IP = "45.33.32.88"

    def rule(self, conditions, match="all", action=SecurityRule.BLOCK, name="r"):
        return SecurityRule.objects.create(name=name, match=match, action=action, conditions=conditions)

    def hit(self, n=1, path="/accounts/login/", ip=None, **kw):
        out = None
        for _ in range(n):
            out = self.client.get(path, HTTP_X_FORWARDED_FOR=ip or self.IP, **kw)
        return out

    def test_activity_rule_blocks_on_the_tripping_request_and_after(self):
        self.rule([{"type": "activity", "count": 3, "minutes": 10}])
        self.assertEqual(self.hit(2).status_code, 200)
        self.assertEqual(self.hit(1).status_code, 403)  # the request that trips it is refused too
        v = VisitorIP.objects.get(ip_address=self.IP)
        self.assertTrue(v.blocked)
        self.assertIn("Rule “r”", v.block_reason)
        self.assertEqual(self.hit().status_code, 403)
        self.assertEqual(SecurityRule.objects.get().times_matched, 1)

    def test_block_and_alert_notifies_administrators_once(self):
        boss = make_admin()
        self.rule([{"type": "activity", "count": 2, "minutes": 5}], action=SecurityRule.BLOCK_ALERT)
        self.hit(4)
        self.assertEqual(Notification.objects.filter(recipient=boss, system=True, verb__contains="blocked").count(), 1)

    def test_alert_only_rule_never_blocks_and_does_not_spam(self):
        boss = make_admin()
        self.rule([{"type": "activity", "count": 2, "minutes": 5}], action=SecurityRule.ALERT)
        self.assertEqual(self.hit(6).status_code, 200)
        self.assertFalse(VisitorIP.objects.get(ip_address=self.IP).blocked)
        self.assertEqual(Notification.objects.filter(recipient=boss, system=True).count(), 1)

    def test_disabled_rule_does_nothing(self):
        r = self.rule([{"type": "activity", "count": 1, "minutes": 5}])
        r.enabled = False
        r.save()
        self.assertEqual(self.hit(3).status_code, 200)

    def test_all_vs_any(self):
        self.rule([{"type": "path", "text": "login"}, {"type": "user_agent", "text": "evilbot"}], match="all", name="and")
        self.assertEqual(self.hit(2).status_code, 200)  # path matches, agent doesn't
        self.assertEqual(self.hit(1, HTTP_USER_AGENT="evilbot/1.0").status_code, 403)
        SecurityRule.objects.all().delete()
        VisitorIP.unblock(VisitorIP.objects.all())
        self.rule([{"type": "path", "text": "zzz-nope"}, {"type": "user_agent", "text": "evilbot"}], match="any", name="or")
        self.assertEqual(self.hit(1, ip="45.33.32.89").status_code, 200)
        self.assertEqual(self.hit(1, ip="45.33.32.89", HTTP_USER_AGENT="evilbot/1.0").status_code, 403)

    def test_country_rules_need_a_known_country(self):
        self.rule([{"type": "outside_countries", "countries": ["US"]}])
        self.assertEqual(self.hit(3).status_code, 200)  # unknown country never matches
        VisitorIP.objects.filter(ip_address=self.IP).update(country="RU", located=True)
        self.assertEqual(self.hit().status_code, 403)

    def test_us_visitors_pass_an_outside_us_rule(self):
        self.rule([{"type": "outside_countries", "countries": ["US"]}])
        VisitorIP.objects.create(ip_address=self.IP, country="US", located=True)
        self.assertEqual(self.hit(5).status_code, 200)

    def test_in_countries_and_unknown_country_conditions(self):
        v = VisitorIP(ip_address=self.IP, country="", located=True)
        self.assertTrue(rules.condition_true({"type": "unknown_country"}, v))
        v.located = False
        self.assertFalse(rules.condition_true({"type": "unknown_country"}, v))
        v.country, v.located = "CA", True
        self.assertTrue(rules.condition_true({"type": "in_countries", "countries": ["CA", "MX"]}, v))
        self.assertFalse(rules.condition_true({"type": "in_countries", "countries": ["US"]}, v))

    def test_failed_logins_rule(self):
        make_user("alice")
        self.rule([{"type": "failed_logins", "count": 3, "minutes": 10}])
        for _ in range(3):
            r = self.client.post(reverse("login"), {"username": "alice", "password": "x"}, HTTP_X_FORWARDED_FOR=self.IP)
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post(reverse("login"), {"username": "alice", "password": PW}, HTTP_X_FORWARDED_FOR=self.IP).status_code, 403)

    def test_total_visits_rule(self):
        self.rule([{"type": "total_visits", "count": 4}])
        self.assertEqual(self.hit(3).status_code, 200)
        self.assertEqual(self.hit(1).status_code, 403)

    def test_whitelisted_and_private_addresses_are_ignored_by_rules(self):
        self.rule([{"type": "activity", "count": 1, "minutes": 5}])
        cfg = SecurityConfig.get()
        cfg.whitelist = f"{self.IP}  # me"
        cfg.save()
        self.assertEqual(self.hit(5).status_code, 200)
        self.assertEqual(self.hit(5, ip="10.9.8.7").status_code, 200)

    def test_old_activity_falls_out_of_the_window(self):
        v = VisitorIP.objects.create(ip_address=self.IP, recent_hits=[int(time.time()) - 3600] * 50)
        self.assertFalse(rules.condition_true({"type": "activity", "count": 5, "minutes": 10}, v))
        self.assertTrue(rules.condition_true({"type": "activity", "count": 5, "minutes": 120}, v))

    def test_clean_rule_validation(self):
        ok = {"name": " Busy  ", "match": "all", "action": "block", "conditions": [{"type": "activity", "count": "5", "minutes": "10"}]}
        fields, err = rules.clean_rule(ok)
        self.assertIsNone(err)
        self.assertEqual(fields["name"], "Busy")
        self.assertEqual(fields["conditions"], [{"type": "activity", "count": 5, "minutes": 10}])
        bad = [
            {**ok, "name": ""}, {**ok, "conditions": []}, {**ok, "match": "xor"}, {**ok, "action": "nuke"},
            {**ok, "conditions": [{"type": "activity", "count": 0, "minutes": 10}]},
            {**ok, "conditions": [{"type": "activity", "count": 5, "minutes": 99999}]},
            {**ok, "conditions": [{"type": "outside_countries", "countries": "USA"}]},
            {**ok, "conditions": [{"type": "path", "text": "x"}]},
            {**ok, "conditions": [{"type": "bogus"}]},
            {**ok, "conditions": [{"type": "path", "text": "ab"}] * 11},
        ]
        for data in bad:
            self.assertIsNotNone(rules.clean_rule(data)[1], data)

    def test_describe_is_plain_english(self):
        r = SecurityRule(name="x", match="any", conditions=[{"type": "outside_countries", "countries": ["US"]}, {"type": "total_visits", "count": 9}])
        self.assertEqual(rules.describe(r), "outside US or 9+ visits in total")

    def test_preview_counts_matches_without_blocking(self):
        VisitorIP.objects.create(ip_address=self.IP, hit_count=50)
        VisitorIP.objects.create(ip_address="45.33.32.99", hit_count=2)
        n, rows = rules.preview(SecurityRule(name="p", match="all", conditions=[{"type": "total_visits", "count": 10}]))
        self.assertEqual((n, rows[0]["ip"]), (1, self.IP))
        self.assertFalse(VisitorIP.objects.get(ip_address=self.IP).blocked)


class WhitelistUnit(TestCase):
    def test_parse_and_match(self):
        nets, bad = rules.parse_whitelist("45.33.32.0/24 # office\nnot-an-ip\n2001:db8::/32\n\n45.33.33.4")
        self.assertEqual(len(nets), 3)
        self.assertEqual(bad, ["not-an-ip"])
        text = "45.33.32.0/24\n2001:db8::/32"
        self.assertTrue(rules.is_whitelisted("45.33.32.9", text))
        self.assertTrue(rules.is_whitelisted("2001:db8::1", text))
        self.assertFalse(rules.is_whitelisted("45.33.33.1", text))
        self.assertFalse(rules.is_whitelisted("garbage", text))
        self.assertFalse(rules.is_whitelisted("", text))

    def test_block_removes_exact_whitelist_line_but_not_a_covering_range(self):
        cfg = SecurityConfig.get()
        cfg.whitelist = "45.33.32.5  # me\n45.33.33.0/24"
        cfg.save()
        a = VisitorIP.objects.create(ip_address="45.33.32.5")
        b = VisitorIP.objects.create(ip_address="45.33.33.9")
        count, stuck = rules.block_ips(VisitorIP.objects.all(), "test")
        self.assertEqual(count, 1)
        self.assertEqual(stuck, [("45.33.33.9", "45.33.33.0/24")])
        a.refresh_from_db()
        self.assertTrue(a.blocked)
        self.assertNotIn("45.33.32.5", SecurityConfig.get().whitelist)

    def test_add_to_whitelist_unblocks(self):
        v = VisitorIP.objects.create(ip_address="45.33.32.5", blocked=True)
        self.assertEqual(rules.add_to_whitelist(["45.33.32.5"], "boss"), 1)
        v.refresh_from_db()
        self.assertFalse(v.blocked)
        self.assertEqual(rules.add_to_whitelist(["45.33.32.5"]), 0)  # no duplicate line


class IpLocateTests(TestCase):
    def setUp(self):
        iplocate.reset_cache()

    def tearDown(self):
        iplocate.reset_cache()

    def test_missing_database_means_unknown_not_an_error(self):
        self.assertIsNone(iplocate.locate_parts("8.8.8.8"))
        self.assertEqual(iplocate.locate_label("8.8.8.8"), "")

    def test_private_addresses_are_never_looked_up(self):
        with mock.patch.object(iplocate, "_get_reader") as reader:
            self.assertIsNone(iplocate.locate_parts("192.168.1.1"))
            self.assertIsNone(iplocate.locate_parts("127.0.0.1"))
            self.assertIsNone(iplocate.locate_parts("nonsense"))
            reader.assert_not_called()

    def test_parses_a_database_record(self):
        fake = mock.Mock()
        fake.get.return_value = {"city": {"names": {"en": "Denver"}}, "subdivisions": [{"names": {"en": "Colorado"}}], "country": {"iso_code": "US"}}
        with mock.patch.object(iplocate, "_get_reader", return_value=fake):
            self.assertEqual(iplocate.locate_parts("8.8.8.8"), {"city": "Denver", "region": "Colorado", "country": "US"})
            self.assertEqual(iplocate.locate_label("8.8.8.8"), "Denver, Colorado, US")

    def test_visitor_gets_located_and_country_rule_then_works(self):
        fake = mock.Mock()
        fake.get.return_value = {"country": {"iso_code": "RU"}}
        SecurityRule.objects.create(name="geo", action="block", conditions=[{"type": "outside_countries", "countries": ["US"]}])
        with mock.patch.object(iplocate, "_get_reader", return_value=fake):
            r = self.client.get("/accounts/login/", HTTP_X_FORWARDED_FOR="45.33.32.123")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(VisitorIP.objects.get(ip_address="45.33.32.123").country, "RU")

    def test_lookup_error_is_swallowed(self):
        fake = mock.Mock()
        fake.get.side_effect = RuntimeError("corrupt")
        with mock.patch.object(iplocate, "_get_reader", return_value=fake):
            self.assertIsNone(iplocate.locate_parts("8.8.8.8"))


# --------------------------------------------------------------------------- the staff portal
class PortalAccessTests(TestCase):
    URLS = ["security_home", "security_locked", "security_people", "security_visitors", "security_trending",
            "security_settings", "security_rules", "security_rule_new"]

    def test_anonymous_goes_to_login(self):
        for name in self.URLS:
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])

    def test_ordinary_members_are_refused(self):
        self.client.force_login(make_user("alice"))
        for name in self.URLS:
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_staff_see_operational_pages_but_not_rules_or_settings(self):
        self.client.force_login(make_staff())
        for name in ("security_home", "security_locked", "security_people", "security_visitors"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        for name in ("security_settings", "security_rules", "security_rule_new", "security_trending"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403, name)

    def test_administrators_see_everything(self):
        self.client.force_login(make_admin())
        for name in self.URLS:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)

    def test_nav_link_only_for_staff(self):
        self.client.force_login(make_user("alice"))
        self.assertNotContains(self.client.get(reverse("home")), 'href="/security/"')
        self.client.force_login(make_staff())
        self.assertContains(self.client.get(reverse("home")), 'href="/security/"')

    def test_staff_cannot_post_to_admin_only_endpoints(self):
        self.client.force_login(make_staff())
        r = self.client.post(reverse("security_settings"), {})
        self.assertEqual(r.status_code, 403)
        rule = SecurityRule.objects.create(name="x", conditions=[{"type": "total_visits", "count": 5}])
        self.assertEqual(self.client.post(reverse("security_rule_action", args=[rule.id]), {"action": "delete"}).status_code, 403)
        self.assertTrue(SecurityRule.objects.exists())

    def test_history_filters(self):
        make_user("alice")
        login_post(Client(), "alice", PW, ip="45.33.33.31")
        login_post(Client(), "alice", "bad", ip="45.33.33.32")
        login_post(Client(), "mallory", "bad", ip="45.33.33.33")
        self.client.force_login(make_staff())
        page = lambda **q: self.client.get(reverse("security_home"), q).content.decode()
        self.assertIn("45.33.33.32", page(result="failed"))
        self.assertNotIn("45.33.33.31", page(result="failed"))
        self.assertIn("45.33.33.33", page(q="mallory"))
        self.assertNotIn("45.33.33.32", page(q="mallory"))
        self.assertIn("45.33.33.31", page(result="ok"))
        self.assertIn("45.33.33.33", page(result="unknown_user"))
        self.assertIn("45.33.33.33", page(q="45.33.33.33"))


class PortalPeopleTests(TestCase):
    def setUp(self):
        self.staff = make_staff()
        self.boss = make_admin()
        self.alice = make_user("alice")

    def act(self, actor, target, action):
        self.client.force_login(actor)
        return self.client.post(reverse("security_people"), {"user_id": target.id, "action": action}, follow=True)

    def test_staff_can_manage_ordinary_members(self):
        self.act(self.staff, self.alice, "deactivate")
        self.alice.refresh_from_db()
        self.assertFalse(self.alice.is_active)
        self.act(self.staff, self.alice, "activate")
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.is_active)

    def test_deactivating_signs_the_person_out(self):
        a = Client()
        login_post(a, "alice", PW)
        self.act(self.staff, self.alice, "deactivate")
        self.assertEqual(a.get(reverse("home")).status_code, 302)

    def test_sign_out_everywhere_action(self):
        a = Client()
        login_post(a, "alice", PW)
        self.act(self.staff, self.alice, "sign_out")
        self.assertEqual(a.get(reverse("home")).status_code, 302)
        self.assertTrue(User.objects.get(pk=self.alice.pk).is_active)

    def test_staff_cannot_touch_other_staff_or_administrators(self):
        other = make_staff("other")
        for target in (other, self.boss):
            r = self.act(self.staff, target, "deactivate")
            self.assertContains(r, "Only an administrator")
            target.refresh_from_db()
            self.assertTrue(target.is_active)

    def test_administrator_can_manage_staff(self):
        self.act(self.boss, self.staff, "require_change")
        self.assertTrue(AccountSecurity.objects.get(user=self.staff).must_change_password)

    def test_nobody_can_deactivate_or_force_themselves(self):
        r = self.act(self.boss, self.boss, "deactivate")
        self.assertContains(r, "your own account")
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.is_active)

    def test_unlock_action(self):
        LoginLockout.objects.create(username="alice", failed_attempts=5, locked_until=timezone.now() + timedelta(minutes=9))
        self.act(self.staff, self.alice, "unlock")
        self.assertFalse(LoginLockout.objects.get().is_locked)

    def test_search(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse("security_people"), {"q": "alic"})
        self.assertContains(r, "alice")
        self.assertNotContains(r, "boss")


class PortalVisitorTests(TestCase):
    def setUp(self):
        self.staff, self.boss = make_staff(), make_admin()
        self.v1 = VisitorIP.objects.create(ip_address="45.33.32.5", hit_count=10, country="DE")
        self.v2 = VisitorIP.objects.create(ip_address="45.33.33.5", hit_count=2)

    def post(self, user, action, *vs):
        self.client.force_login(user)
        return self.client.post(reverse("security_visitors"), {"action": action, "ids": [v.id for v in vs]}, follow=True,
                                HTTP_X_FORWARDED_FOR="45.33.34.99")

    def test_block_and_unblock(self):
        self.post(self.staff, "block", self.v1)
        self.v1.refresh_from_db()
        self.assertTrue(self.v1.blocked)
        self.assertIn("staffer", self.v1.block_reason)
        self.post(self.staff, "unblock", self.v1)
        self.v1.refresh_from_db()
        self.assertFalse(self.v1.blocked)

    def test_you_cannot_block_your_own_address(self):
        me = VisitorIP.objects.create(ip_address="45.33.34.99")
        self.post(self.staff, "block", me)
        me.refresh_from_db()
        self.assertFalse(me.blocked)

    def test_only_administrators_can_whitelist(self):
        self.post(self.staff, "whitelist", self.v1)
        self.assertEqual(SecurityConfig.get().whitelist, "")
        self.post(self.boss, "whitelist", self.v1)
        self.assertIn("45.33.32.5", SecurityConfig.get().whitelist)

    def test_blocking_a_range_covered_address_warns(self):
        cfg = SecurityConfig.get()
        cfg.whitelist = "45.33.32.0/24"
        cfg.save()
        r = self.post(self.staff, "block", self.v1)
        self.assertContains(r, "stays unblocked")

    def test_filters_and_sorting(self):
        self.client.force_login(self.staff)
        r = self.client.get(reverse("security_visitors"), {"q": "DE"})
        self.assertContains(r, "45.33.32.5")
        self.assertNotContains(r, "45.33.33.5")
        VisitorIP.block(VisitorIP.objects.filter(pk=self.v2.pk), "x")
        r = self.client.get(reverse("security_visitors"), {"blocked": "1", "sort": "-last_seen"})
        self.assertContains(r, "45.33.33.5")
        self.assertNotContains(r, "45.33.32.5")
        self.assertEqual(self.client.get(reverse("security_visitors"), {"sort": "id; DROP TABLE"}).status_code, 200)


class PortalSettingsAndRulesTests(TestCase):
    def setUp(self):
        self.boss = make_admin()
        self.client.force_login(self.boss)

    def test_settings_validate_the_whitelist(self):
        base = {"password_reuse_block": 1, "login_history_days": 30, "tracking_days": 30}
        r = self.client.post(reverse("security_settings"), {**base, "whitelist": "45.33.32.1\nnot-an-ip"})
        self.assertContains(r, "not IP addresses")
        r = self.client.post(reverse("security_settings"), {**base, "whitelist": "45.33.32.0/24 # hq"}, follow=True)
        self.assertContains(r, "saved")
        self.assertEqual(SecurityConfig.get().login_history_days, 30)

    def test_saving_the_whitelist_unblocks_covered_addresses(self):
        VisitorIP.objects.create(ip_address="45.33.32.9", blocked=True)
        self.client.post(reverse("security_settings"), {"password_reuse_block": 1, "login_history_days": 30, "tracking_days": 30,
                                                       "whitelist": "45.33.32.0/24"})
        self.assertFalse(VisitorIP.objects.get().blocked)

    def rule_post(self, **over):
        data = {"name": "Busy bots", "enabled": "on", "match": "all", "action": "block_alert",
                "use_activity": "on", "activity_count": "20", "activity_minutes": "5"}
        data.update(over)
        return self.client.post(reverse("security_rule_new"), data, follow=True)

    def test_create_edit_toggle_delete_rule(self):
        r = self.rule_post()
        self.assertContains(r, "saved")
        rule = SecurityRule.objects.get()
        self.assertEqual(rule.conditions, [{"type": "activity", "count": 20, "minutes": 5}])
        self.assertContains(self.client.get(reverse("security_rules")), "20+ visits or sign-in tries within 5 min")
        edit = self.client.get(reverse("security_rule_edit", args=[rule.id]))
        self.assertContains(edit, 'value="20"')
        self.client.post(reverse("security_rule_edit", args=[rule.id]), {
            "name": "Renamed", "match": "any", "action": "alert", "use_path": "on", "path_text": "wp-login"})
        rule.refresh_from_db()
        self.assertEqual((rule.name, rule.match, rule.action, rule.enabled), ("Renamed", "any", "alert", False))
        self.assertEqual(rule.conditions, [{"type": "path", "text": "wp-login"}])
        self.client.post(reverse("security_rule_action", args=[rule.id]), {"action": "toggle"})
        rule.refresh_from_db()
        self.assertTrue(rule.enabled)
        self.client.post(reverse("security_rule_action", args=[rule.id]), {"action": "delete"})
        self.assertFalse(SecurityRule.objects.exists())

    def test_rule_needs_a_condition_and_valid_numbers(self):
        self.assertContains(self.rule_post(use_activity=""), "at least one condition")
        self.assertContains(self.rule_post(activity_count="0"), "enter how many")
        self.assertContains(self.rule_post(use_outside_countries="on", outside_countries_countries="USA"), "two-letter")
        self.assertFalse(SecurityRule.objects.exists())

    def test_preview_does_not_save(self):
        VisitorIP.objects.create(ip_address="45.33.32.9", hit_count=500)
        r = self.client.post(reverse("security_rule_new"), {
            "name": "p", "match": "all", "action": "block", "use_total_visits": "on", "total_visits_count": "100", "preview": "1"})
        self.assertContains(r, "would match 1 address")
        self.assertFalse(SecurityRule.objects.exists())


# --------------------------------------------------------------------------- trending
class TrendingTests(TestCase):
    def view(self, path="/accounts/login/", ip="45.33.33.1", **kw):
        return self.client.get(path, HTTP_X_FORWARDED_FOR=ip, **kw)

    def test_page_views_are_recorded_with_visitor_cookie_device_and_referrer(self):
        r = self.view(HTTP_USER_AGENT=UA_IPHONE, HTTP_REFERER="https://news.example.org/story")
        pv = PageView.objects.get()
        self.assertEqual((pv.path, pv.device_type, pv.referrer, pv.ip_address), ("/accounts/login/", "phone", "news.example.org", "45.33.33.1"))
        self.assertEqual(len(r.cookies["lc_vid"].value), 32)
        self.assertTrue(r.cookies["lc_vid"]["httponly"])

    def test_same_visitor_keeps_one_id(self):
        self.view()
        self.view("/accounts/signup/")
        self.assertEqual(PageView.objects.values("visitor_id").distinct().count(), 1)

    def test_forged_cookie_is_replaced(self):
        self.client.cookies["lc_vid"] = "<script>"
        self.view()
        self.assertNotEqual(PageView.objects.get().visitor_id, "<script>")

    def test_do_not_track_and_gpc_are_respected(self):
        self.view(HTTP_DNT="1")
        self.view(HTTP_SEC_GPC="1")
        self.assertEqual(PageView.objects.count(), 0)

    def test_tracking_can_be_switched_off(self):
        cfg = SecurityConfig.get()
        cfg.tracking_enabled = False
        cfg.save()
        self.view()
        self.assertEqual(PageView.objects.count(), 0)

    def test_not_recorded_for_static_admin_health_security_redirects_posts_and_404(self):
        for p in ("/static/favicon.svg", "/admin/login/", "/healthz/", "/robots.txt", "/nope-404/", "/"):
            self.view(p)
        self.client.post(reverse("login"), {}, HTTP_X_FORWARDED_FOR="45.33.33.1")
        self.assertEqual(PageView.objects.count(), 0)

    def test_signed_in_member_is_linked_and_own_site_referrer_is_dropped(self):
        alice = make_user("alice")
        self.client.force_login(alice)
        self.view("/jobs/", HTTP_REFERER="http://testserver/")
        pv = PageView.objects.get()
        self.assertEqual((pv.user, pv.referrer), (alice, ""))

    def test_dashboard_numbers(self):
        alice, bob = make_user("alice"), make_user("bob")
        boss = make_admin()
        post = Post.objects.create(author=alice, body="Hot take on hiring")
        Like.objects.create(post=post, user=bob)
        Comment.objects.create(post=post, user=bob, body="agree")
        Post.objects.create(author=alice, body="quiet post")
        c1, c2 = Client(), Client()
        for c, ip in ((c1, "45.33.33.1"), (c2, "45.33.33.2")):
            c.get("/accounts/login/", HTTP_X_FORWARDED_FOR=ip, HTTP_REFERER="https://news.example.org/")
        c1.get("/accounts/signup/", HTTP_X_FORWARDED_FOR="45.33.33.1")
        self.client.force_login(boss)
        r = self.client.get(reverse("security_trending"), {"days": 7})
        self.assertEqual(r.context["total_views"], 3)
        self.assertEqual(r.context["visitors"], 2)
        self.assertEqual(r.context["new_members"], 3)  # alice, bob, boss just joined
        self.assertEqual(r.context["top_pages"][0]["label"], "/accounts/login/")
        self.assertEqual(r.context["referrers"][0]["label"], "news.example.org")
        self.assertEqual([p.body for p in r.context["hot_posts"]], ["Hot take on hiring"])
        self.assertContains(r, "Hot take on hiring")

    def test_period_filter_excludes_old_views(self):
        PageView.objects.create(visitor_id="a" * 32, path="/old/", created_at=timezone.now() - timedelta(days=40))
        self.client.force_login(make_admin())
        self.assertEqual(self.client.get(reverse("security_trending"), {"days": 7}).context["total_views"], 0)
        self.assertEqual(self.client.get(reverse("security_trending"), {"days": 90}).context["total_views"], 1)
        self.assertEqual(self.client.get(reverse("security_trending"), {"days": "abc"}).status_code, 200)

    def test_staff_access_follows_the_setting(self):
        self.client.force_login(make_staff())
        self.assertEqual(self.client.get(reverse("security_trending")).status_code, 403)
        cfg = SecurityConfig.get()
        cfg.trending_for_staff = True
        cfg.save()
        self.assertEqual(self.client.get(reverse("security_trending")).status_code, 200)
        self.client.force_login(make_user("alice"))
        self.assertEqual(self.client.get(reverse("security_trending")).status_code, 403)


# --------------------------------------------------------------------------- housekeeping
class ChoresTests(TestCase):
    def test_prunes_old_data_only(self):
        alice = make_user("alice")
        old = LoginEvent.objects.create(username="alice", result="ok")
        fresh = LoginEvent.objects.create(username="alice", result="ok")
        LoginEvent.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=400))
        pv_old = PageView.objects.create(visitor_id="a" * 32, path="/")
        PageView.objects.filter(pk=pv_old.pk).update(created_at=timezone.now() - timedelta(days=200))
        PageView.objects.create(visitor_id="b" * 32, path="/")
        UnlockCode.objects.create(user=alice, code_hash="x", expires_at=timezone.now() - timedelta(days=3))
        throttle.record_try("zzz")
        from .models import AttemptThrottle
        AttemptThrottle.objects.update(last_attempt_at=timezone.now() - timedelta(days=3))
        call_command("security_chores")
        self.assertEqual(list(LoginEvent.objects.values_list("pk", flat=True)), [fresh.pk])
        self.assertEqual(PageView.objects.count(), 1)
        self.assertEqual(UnlockCode.objects.count(), 0)
        self.assertEqual(AttemptThrottle.objects.count(), 0)

    def test_zero_days_means_keep_forever(self):
        cfg = SecurityConfig.get()
        cfg.login_history_days = 0
        cfg.tracking_days = 0
        cfg.save()
        e = LoginEvent.objects.create(username="a", result="ok")
        LoginEvent.objects.filter(pk=e.pk).update(created_at=timezone.now() - timedelta(days=9999))
        call_command("security_chores")
        self.assertEqual(LoginEvent.objects.count(), 1)
