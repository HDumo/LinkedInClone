"""Login history, lockout, password rules and unlock codes.

Passwords are never stored or logged: Django keeps only a salted, slow hash
(Argon2), and password history keeps those hashes. Unlock codes are hashed the
same way and expire in 10 minutes."""
import logging
import secrets
from datetime import timedelta

from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone

from core.ip import client_ip
from notifications.models import notify_admins, notify_system

from .devices import device_label
from .iplocate import locate_label

logger = logging.getLogger(__name__)

CODE_MINUTES = 10
CODE_TRIES = 5
CODES_PER_15_MIN = 3
CODES_PER_DAY = 10
GRACE_ATTEMPTS = 3
BACKOFF_MINUTES = [1, 2, 3, 5, 10, 30, 60]


def _page(request):
    path = getattr(request, "path", "") or ""
    return "admin" if path.startswith("/admin/") else "member"


def log_login(request, username, result, user=None):
    """Save one LoginEvent. Never raises: logging must not block a sign-in."""
    from .models import LoginEvent

    try:
        ip = client_ip(request) if request is not None else None
        return LoginEvent.objects.create(
            user=user, username=(username or "")[:150], result=result, page=_page(request), ip_address=ip,
            location=locate_label(ip) if ip else "",
            device=device_label((request.META.get("HTTP_USER_AGENT") if request is not None else "") or "")[:60],
        )
    except Exception:
        logger.exception("Could not record login event")
        return None


def _new_place(event):
    """True if this user has signed in before, but never from this place
    (location, or IP when unknown) on this device."""
    from .models import LoginEvent

    earlier = LoginEvent.objects.filter(user=event.user, result=LoginEvent.OK).exclude(pk=event.pk)
    if not earlier.exists():
        return False
    where = {"location": event.location} if event.location else {"ip_address": event.ip_address}
    return not earlier.filter(device=event.device, **where).exists()


@receiver(user_logged_in, dispatch_uid="security_log_login")
def on_login(sender, request, user, **kwargs):
    from .models import LoginEvent, UserSession

    event = log_login(request, user.get_username(), LoginEvent.OK, user)
    if event and _new_place(event):
        where = event.location or f"IP {event.ip_address}"
        notify_system(user, f"New sign-in from {event.device} ({where}). If that wasn't you, change your password.",
                      reverse("signed_in_devices"))
    if request is not None and hasattr(request, "session"):
        if not request.session.session_key:
            request.session.save()
        UserSession.objects.update_or_create(
            session_key=request.session.session_key,
            defaults={"user": user, "device": device_label(request.META.get("HTTP_USER_AGENT"))[:120],
                      "ip_address": client_ip(request), "last_seen": timezone.now()})


@receiver(user_logged_out, dispatch_uid="security_forget_session")
def on_logout(sender, request, user, **kwargs):
    from .models import UserSession

    if request is not None and getattr(request, "session", None) is not None and request.session.session_key:
        UserSession.objects.filter(session_key=request.session.session_key).delete()


@receiver(user_login_failed, dispatch_uid="security_log_failed_login")
def on_login_failed(sender, credentials, request=None, **kwargs):
    from .models import LoginEvent

    username = (credentials or {}).get("username") or ""
    user = get_user_model().objects.filter(username=username).first()
    if user is None:
        result = LoginEvent.UNKNOWN_USER
    elif not user.is_active:
        result = LoginEvent.INACTIVE
    else:
        result = LoginEvent.BAD_PASSWORD
    event = log_login(request, username, result, user)
    if user is not None and (user.is_staff or user.is_superuser) and result == LoginEvent.BAD_PASSWORD:
        where = (event.location or f"IP {event.ip_address}") if event else "an unknown place"
        notify_admins(f"Failed sign-in for staff account {user.get_username()} from {where}",
                      reverse("security_home") + f"?q={user.get_username()}")


# --- Per-account lockout -----------------------------------------------------

def _get_user(username):
    return get_user_model().objects.filter(username=username).first()


def _lockout_message(locked_until):
    local = timezone.localtime(locked_until)
    wait_minutes = max(1, (int((locked_until - timezone.now()).total_seconds()) + 59) // 60)
    return (f"Too many failed attempts for this account. Try again in about {wait_minutes} "
            f"minute{'s' if wait_minutes != 1 else ''}, at {local:%-I:%M %p}, or unlock it now with a code "
            f"sent to your email (“Locked out?” below), or reset your password.")


def check_lockout(username):
    """Raises ValidationError (never authenticating, even with the right
    password) if this username is currently locked out."""
    from .models import LoginLockout

    lockout = LoginLockout.objects.filter(username=username).first()
    if lockout and lockout.is_locked:
        raise ValidationError(_lockout_message(lockout.locked_until), code="locked")


def record_failed_attempt(username):
    """Counts a failure. Returns the message to show: None for an early
    failure, a countdown as the limit nears, or the lockout message."""
    from .models import LoginLockout

    lockout, _ = LoginLockout.objects.get_or_create(username=username, defaults={"user": _get_user(username)})
    lockout.failed_attempts += 1
    lockout.last_failed_at = timezone.now()
    attempt = lockout.failed_attempts
    if attempt > GRACE_ATTEMPTS:
        idx = min(attempt - GRACE_ATTEMPTS - 1, len(BACKOFF_MINUTES) - 1)
        lockout.locked_until = timezone.now() + timedelta(minutes=BACKOFF_MINUTES[idx])
        lockout.save()
        if attempt == GRACE_ATTEMPTS + 1:  # tell administrators once, when it first locks
            user = _get_user(username)
            if user is not None:
                when = timezone.localtime(lockout.locked_until).strftime("%-I:%M %p")
                notify_admins(f"Account {username} was locked until {when} after repeated wrong passwords.",
                              reverse("security_locked"))
        return _lockout_message(lockout.locked_until)
    lockout.save()
    if attempt >= 2:
        remaining = GRACE_ATTEMPTS - attempt + 1
        return (f"Incorrect username or password. {remaining} attempt{'s' if remaining != 1 else ''} "
                f"remaining before your account is locked.")
    return None


def record_successful_login(username):
    from .models import LoginLockout

    LoginLockout.objects.filter(username=username).update(failed_attempts=0, locked_until=None)


class LockoutAwareFormMixin:
    """Per-account lockout with escalating backoff and attempts-remaining
    warnings, in front of the normal credential check."""

    def clean(self):
        username = self.cleaned_data.get("username")
        if username:
            try:
                check_lockout(username)
            except ValidationError:
                from .models import LoginEvent

                log_login(getattr(self, "request", None), username, LoginEvent.LOCKED, _get_user(username))
                raise
        try:
            cleaned = super().clean()
        except ValidationError:
            if username:
                message = record_failed_attempt(username)
                if message:
                    raise ValidationError(message)
            raise
        if username:
            record_successful_login(username)
        return cleaned


class LockoutAuthenticationForm(LockoutAwareFormMixin, AuthenticationForm):
    """Used by the member sign-in page."""


class LockoutAdminAuthenticationForm(LockoutAwareFormMixin, AdminAuthenticationForm):
    """Used by /admin/login/."""


def unlock_account(user):
    from .models import LoginLockout

    LoginLockout.objects.filter(username=user.get_username()).update(failed_attempts=0, locked_until=None)


# --- Password rules ----------------------------------------------------------

class PasswordReuseValidator:
    """Refuses a recent password when Security → Settings says so, and keeps
    the last 5 password hashes as passwords change."""

    def validate(self, password, user=None):
        if user is None or not getattr(user, "pk", None):
            return
        from .models import PasswordHistory, SecurityConfig

        count = SecurityConfig.get().password_reuse_block
        if not count:
            return
        current = get_user_model().objects.filter(pk=user.pk).values_list("password", flat=True).first()
        hashes = ([current] if current else []) + list(
            PasswordHistory.objects.filter(user_id=user.pk).values_list("password", flat=True)[:count])
        for old in hashes[:count] if count == 1 else hashes:
            if old and check_password(password, old):
                raise ValidationError(
                    "You used this password recently. Please choose a different one." if count > 1
                    else "That's your current password. Please choose a different one.",
                    code="password_reused")

    def password_changed(self, password, user=None):
        if user is None or not getattr(user, "pk", None) or not user.password:
            return
        from .models import AccountSecurity, PasswordHistory

        PasswordHistory.objects.create(user_id=user.pk, password=user.password)
        stale = PasswordHistory.objects.filter(user_id=user.pk).values_list("pk", flat=True)[5:]
        PasswordHistory.objects.filter(pk__in=list(stale)).delete()
        AccountSecurity.objects.update_or_create(
            user_id=user.pk, defaults={"password_changed_at": timezone.now(), "must_change_password": False})
        unlock_account(user)  # a new password also clears a lockout

    def get_help_text(self):
        return "Your password can't be one you used recently."


def must_change_password(user):
    if not getattr(user, "is_authenticated", False):
        return False
    security = getattr(user, "security", None)
    return bool(security and security.must_change_password)


# --- Unlock codes ------------------------------------------------------------

def issue_unlock_code(user):
    """Emails a fresh 6-digit code. Returns False if too many were sent
    recently (the page doesn't say so, to avoid revealing accounts)."""
    from .models import UnlockCode

    now = timezone.now()
    recent = UnlockCode.objects.filter(user=user)
    if (recent.filter(created_at__gte=now - timedelta(minutes=15)).count() >= CODES_PER_15_MIN
            or recent.filter(created_at__gte=now - timedelta(days=1)).count() >= CODES_PER_DAY):
        return False
    UnlockCode.objects.filter(user=user, used_at__isnull=True).update(used_at=now)  # older codes stop working
    code = f"{secrets.randbelow(1_000_000):06d}"
    UnlockCode.objects.create(user=user, code_hash=make_password(code), expires_at=now + timedelta(minutes=CODE_MINUTES))
    try:
        send_mail(
            "Your LinkedClone unlock code",
            f"Hello {user.first_name or user.get_username()},\n\nYour unlock code is {code}. It works for "
            f"{CODE_MINUTES} minutes.\n\nIf you didn't ask for this, ignore this email; your account stays safe.\n",
            None, [user.email], fail_silently=False)
    except Exception:
        logger.exception("Unlock code email failed for user %s", user.pk)
    return True


def check_unlock_code(user, code):
    """True (and the code is used up) if it matches; wrong tries count."""
    from .models import UnlockCode

    entry = UnlockCode.objects.filter(user=user, used_at__isnull=True, expires_at__gt=timezone.now()).first()
    if entry is None or entry.attempts >= CODE_TRIES:
        return False
    if check_password((code or "").strip(), entry.code_hash):
        entry.used_at = timezone.now()
        entry.save(update_fields=["used_at"])
        return True
    entry.attempts += 1
    entry.save(update_fields=["attempts"])
    return False


def prune_all():
    """Daily chores: old login history, expired codes, throttles, rate counters, page views."""
    from core import ratecache

    from . import throttle
    from .models import LoginEvent, PageView, SecurityConfig, UnlockCode

    cfg = SecurityConfig.get()
    out = {}
    out["login events"] = (LoginEvent.objects.filter(created_at__lt=timezone.now() - timedelta(days=cfg.login_history_days)).delete()[0]
                           if cfg.login_history_days else 0)
    out["unlock codes"] = UnlockCode.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()[0]
    out["attempt counters"] = throttle.prune()
    out["rate counters"] = ratecache.prune()
    out["page views"] = (PageView.objects.filter(created_at__lt=timezone.now() - timedelta(days=cfg.tracking_days)).delete()[0]
                         if cfg.tracking_days else 0)
    return out
