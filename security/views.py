"""Member pages: signed-in devices, my sign-in activity, unlock with a code."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from core.ip import client_ip

from . import throttle
from .devices import active_sessions, sign_out_everywhere, sign_out_sessions
from .events import LockoutAuthenticationForm, check_unlock_code, issue_unlock_code, log_login, unlock_account
from .models import LoginEvent


class SecureLoginView(auth_views.LoginView):
    form_class = LockoutAuthenticationForm


class SecurePasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    """A password reset also signs the person out everywhere, so whoever may
    have had the old password loses access."""

    def form_valid(self, form):
        response = super().form_valid(form)
        sign_out_everywhere(form.user)
        return response


class RequiredNoticePasswordChangeView(auth_views.PasswordChangeView):
    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["required"] = self.request.GET.get("required") == "1"
        return ctx


@login_required
@require_http_methods(["GET", "POST"])
def signed_in_devices(request):
    current_key = request.session.session_key
    if request.method == "POST":
        others = [r for r in active_sessions(request.user) if r.session_key != current_key]
        action = request.POST.get("action")
        if action == "sign_out_others":
            sign_out_sessions(others)
            messages.success(request, "Signed out of your other devices.")
        elif action == "sign_out_one":
            target = [r for r in others if str(r.pk) == request.POST.get("device_id", "")]
            sign_out_sessions(target)
            if target:
                messages.success(request, f"Signed out of {target[0].device}.")
        return redirect("signed_in_devices")
    rows = active_sessions(request.user)
    return render(request, "security/devices.html", {
        "current": next((r for r in rows if r.session_key == current_key), None),
        "others": [r for r in rows if r.session_key != current_key],
    })


@login_required
def my_activity(request):
    events = LoginEvent.objects.filter(Q(user=request.user) | Q(username=request.user.get_username()))[:50]
    return render(request, "security/activity.html", {"events": events})


def _wait_text(seconds):
    minutes = max(1, (seconds + 59) // 60)
    return f"Too many tries. Please wait about {minutes} minute{'s' if minutes != 1 else ''} and try again."


def _find_user(identifier):
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    User = get_user_model()
    user = User.objects.filter(username__iexact=identifier, is_active=True).first()
    if user is None and "@" in identifier:
        user = User.objects.filter(email__iexact=identifier, is_active=True).order_by("id").first()
    return user


@require_http_methods(["GET", "POST"])
def unlock_request(request):
    """Step 1: ask for a code. Always answers the same way, so it can't be
    used to find out which accounts exist."""
    if request.method == "POST":
        identifier = request.POST.get("identifier", "").strip()[:150]
        keys = (f"unlock-ip:{client_ip(request)}", f"unlock-id:{identifier}")
        wait = throttle.wait_seconds(*keys)
        if wait:
            messages.error(request, _wait_text(wait))
            return redirect("unlock_request")
        throttle.record_try(*keys)
        user = _find_user(identifier)
        if user is not None and user.email:
            issue_unlock_code(user)
        request.session["unlock_identifier"] = identifier
        messages.success(request, "If that account exists and has an email address, we've sent a 6-digit code. It works for 10 minutes.")
        return redirect("unlock_confirm")
    return render(request, "security/unlock_request.html")


@require_http_methods(["GET", "POST"])
def unlock_confirm(request):
    identifier = request.session.get("unlock_identifier", "")
    if not identifier:
        return redirect("unlock_request")
    if request.method == "POST":
        keys = (f"code-ip:{client_ip(request)}", f"code-id:{identifier}")
        wait = throttle.wait_seconds(*keys)
        if wait:
            messages.error(request, _wait_text(wait))
            return redirect("unlock_confirm")
        user = _find_user(identifier)
        if user is not None and check_unlock_code(user, request.POST.get("code", "")):
            unlock_account(user)
            throttle.clear(*keys, f"unlock-ip:{client_ip(request)}", f"unlock-id:{identifier}")
            log_login(request, user.get_username(), LoginEvent.OTP_UNLOCK, user)
            request.session.pop("unlock_identifier", None)
            messages.success(request, "Your account is unlocked. You can sign in now.")
            return redirect("login")
        throttle.record_try(*keys)
        messages.error(request, "That code didn't work. Check it and try again, or ask for a new one.")
        return redirect("unlock_confirm")
    return render(request, "security/unlock_confirm.html", {"identifier": identifier})
