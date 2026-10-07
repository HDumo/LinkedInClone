"""Visitor logging and blocking, forced password change, device "last seen",
and server-side page-view tracking for Trending."""
import logging
import secrets
import urllib.parse

from django.db.models import F
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone

from core.ip import client_ip

from .devices import SEEN_EVERY, device_label, device_type
from .events import must_change_password

logger = logging.getLogger(__name__)

SKIP_PREFIXES = ("/admin/", "/static/", "/media/", "/healthz/", "/security/", "/robots.txt")


class VisitorLoggingMiddleware:
    """Counts visits per IP address and enforces a block on any address staff
    (or an automatic rule) flagged. The block check runs for EVERY request,
    except /admin/ and /healthz/, which always stay reachable so staff can undo
    an accidental self-block and the server's own health check never fails.
    Private and loopback addresses are never blocked."""

    BLOCK_EXEMPT_PREFIXES = ("/admin/", "/healthz/")
    # Sign-in and code/reset tries count toward the automatic rules too.
    SIGN_IN_PREFIXES = ("/accounts/login/", "/accounts/signup/", "/accounts/unlock/",
                        "/accounts/password_reset/", "/admin/login/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        ip = client_ip(request)
        if ip and not request.path.startswith(self.BLOCK_EXEMPT_PREFIXES) and self._is_blocked(ip):
            return HttpResponseForbidden("Access denied.", content_type="text/plain")
        page_view = request.method == "GET" and not request.path.startswith(SKIP_PREFIXES)
        if page_view or (request.method == "POST" and request.path.startswith(self.SIGN_IN_PREFIXES)):
            if self._record_visit(ip, page_view, request) and not request.path.startswith(self.BLOCK_EXEMPT_PREFIXES):
                return HttpResponseForbidden("Access denied.", content_type="text/plain")  # the request that trips a rule is refused too
        return self.get_response(request)

    def _is_blocked(self, ip):
        try:
            from .models import VisitorIP
            from .rules import is_exempt_address, is_whitelisted

            if is_exempt_address(ip) or not VisitorIP.objects.filter(ip_address=ip, blocked=True).exists():
                return False
            return not is_whitelisted(ip)  # the whitelist always wins
        except Exception:
            # A database hiccup must never turn into every visitor being locked out.
            logger.exception("Failed to check IP block status")
            return False

    def _record_visit(self, ip, page_view, request):
        if not ip:
            return False
        try:
            from .iplocate import locate_parts
            from .models import VisitorIP

            obj, created = VisitorIP.objects.get_or_create(ip_address=ip, defaults={"hit_count": int(page_view)})
            if not created and page_view:
                VisitorIP.objects.filter(pk=obj.pk).update(hit_count=F("hit_count") + 1)
                obj.hit_count += 1
            if not obj.located:
                parts = locate_parts(ip)
                if parts is not None:
                    VisitorIP.objects.filter(pk=obj.pk).update(
                        city=parts["city"][:80], region=parts["region"][:80], country=parts["country"][:2], located=True)
                    obj.city, obj.region, obj.country, obj.located = parts["city"], parts["region"], parts["country"], True
        except Exception:
            # A visit counter is a nice-to-have, never a reason to break a real page load.
            logger.exception("Failed to record visitor IP")
            return False
        from .rules import record_and_check

        return record_and_check(obj, request.path, request.META.get("HTTP_USER_AGENT", "")[:300])


class MustChangePasswordMiddleware:
    """While an administrator requires a new password, the person can only
    reach the change-password page (and sign out)."""

    ALLOWED = ("/accounts/password_change/", "/accounts/logout/", "/static/", "/media/", "/healthz/", "/robots.txt")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and not request.path.startswith(self.ALLOWED):
            try:
                required = must_change_password(user)
            except Exception:
                required = False
            if required:
                return redirect("/accounts/password_change/?required=1")
        return self.get_response(request)


class LastSeenMiddleware:
    """Keeps each device's "last used" time roughly current (at most one write
    per 10 minutes per signed-in session)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        if user is not None and user.is_authenticated and session is not None and session.session_key:
            now = timezone.now()
            last = session.get("_seen")
            if not last or now.timestamp() - last > SEEN_EVERY.total_seconds():
                from .models import UserSession

                # Also covers sessions whose key changed (e.g. after a password change).
                UserSession.objects.update_or_create(session_key=session.session_key, defaults={
                    "user": user, "last_seen": now, "ip_address": client_ip(request),
                    "device": device_label(request.META.get("HTTP_USER_AGENT"))[:120]})
                session["_seen"] = now.timestamp()
        return response


class PageViewTrackingMiddleware:
    """Records one PageView per successful HTML page load for Trending.
    Done on the server, so the site needs no tracking JavaScript. Skipped when
    the browser sends Do Not Track or Global Privacy Control, when tracking is
    switched off, and for static files, the admin and the Security pages."""

    COOKIE = "lc_vid"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if (request.method != "GET" or response.status_code != 200 or request.path.startswith(SKIP_PREFIXES)
                    or "text/html" not in response.get("Content-Type", "")
                    or request.META.get("HTTP_DNT") == "1" or request.META.get("HTTP_SEC_GPC") == "1"):
                return response
            from .models import PageView, SecurityConfig

            if not SecurityConfig.get().tracking_enabled:
                return response
            vid = request.COOKIES.get(self.COOKIE, "")
            if len(vid) != 32 or not vid.isalnum():
                vid = secrets.token_hex(16)
                response.set_cookie(self.COOKIE, vid, max_age=365 * 24 * 3600, httponly=True, samesite="Lax",
                                    secure=request.is_secure())
            ref = urllib.parse.urlparse(request.META.get("HTTP_REFERER", "")).netloc
            if ref == request.get_host():
                ref = ""
            user = request.user if getattr(request, "user", None) is not None and request.user.is_authenticated else None
            PageView.objects.create(
                visitor_id=vid, ip_address=client_ip(request), user=user, path=request.path[:300], referrer=ref[:120],
                device_type=device_type(request.META.get("HTTP_USER_AGENT", "")))
        except Exception:
            logger.exception("Could not record page view")
        return response
