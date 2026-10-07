"""Device labels and signed-in devices: every sign-in records the browser it
came from, so a person can see where else they're signed in and sign those
out."""
import re
from datetime import timedelta

from django.contrib.sessions.models import Session
from django.utils import timezone

SEEN_EVERY = timedelta(minutes=10)


def device_label(user_agent):
    ua = user_agent or ""
    if re.search(r"iPhone", ua):
        os_name = "iPhone"
    elif re.search(r"iPad", ua) or ("Macintosh" in ua and "Mobile" in ua):
        os_name = "iPad"
    elif "Android" in ua:
        os_name = "Android"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name = "Mac"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "an unknown device"
    for name, pattern in (("Edge", r"Edg/"), ("Samsung Internet", r"SamsungBrowser"), ("Firefox", r"Firefox|FxiOS"),
                          ("Chrome", r"Chrome|CriOS"), ("Safari", r"Safari")):
        if re.search(pattern, ua):
            return f"{name} on {os_name}"
    return f"Browser on {os_name}"


def device_type(user_agent):
    ua = user_agent or ""
    if re.search(r"iPad|Tablet", ua):
        return "tablet"
    if re.search(r"Mobi|iPhone|Android", ua):
        return "phone"
    return "computer"


def active_sessions(user):
    """This user's device rows whose session is still valid; stale rows are dropped."""
    from .models import UserSession

    rows = list(UserSession.objects.filter(user=user))
    live = set(Session.objects.filter(session_key__in=[r.session_key for r in rows], expire_date__gt=timezone.now())
               .values_list("session_key", flat=True))
    stale = [r.pk for r in rows if r.session_key not in live]
    if stale:
        UserSession.objects.filter(pk__in=stale).delete()
    return [r for r in rows if r.session_key in live]


def sign_out_sessions(rows):
    from .models import UserSession

    keys = [r.session_key for r in rows]
    Session.objects.filter(session_key__in=keys).delete()
    UserSession.objects.filter(session_key__in=keys).delete()


def sign_out_everywhere(user):
    """After a password reset or deactivation: end every session for this user."""
    from .models import UserSession

    sign_out_sessions(list(UserSession.objects.filter(user=user)))
