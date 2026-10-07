"""The staff Security portal: sign-in history, locked accounts, people,
visitor addresses, automatic rules, settings, and Trending."""
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from feed.models import Post

from core.ip import client_ip

from . import rules
from .devices import sign_out_everywhere
from .events import unlock_account
from .forms import RuleForm, SecurityConfigForm
from .models import (AccountSecurity, LoginEvent, LoginLockout, PageView, SecurityConfig, SecurityRule,
                     VisitorIP)


def staff_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_superuser):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


def _page(request, qs, per=50):
    return Paginator(qs, per).get_page(request.GET.get("page"))


@staff_required
def home(request):
    q = request.GET.get("q", "").strip()
    result = request.GET.get("result", "")
    events = LoginEvent.objects.all()
    if q:
        events = events.filter(Q(username__icontains=q) | Q(ip_address__startswith=q) | Q(location__icontains=q))
    if result == "failed":
        events = events.exclude(result__in=[LoginEvent.OK, LoginEvent.OTP_UNLOCK])
    elif result in dict(LoginEvent.RESULT_CHOICES):
        events = events.filter(result=result)
    day = timezone.now() - timedelta(hours=24)
    return render(request, "security/home.html", {
        "page": _page(request, events), "q": q, "result": result, "results": LoginEvent.RESULT_CHOICES,
        "failed_24h": LoginEvent.objects.filter(created_at__gte=day).exclude(
            result__in=[LoginEvent.OK, LoginEvent.OTP_UNLOCK]).count(),
        "ok_24h": LoginEvent.objects.filter(created_at__gte=day, result=LoginEvent.OK).count(),
        "locked_now": LoginLockout.objects.filter(locked_until__gt=timezone.now()).count(),
        "blocked_ips": VisitorIP.objects.filter(blocked=True).count(),
    })


@staff_required
def locked(request):
    if request.method == "POST":
        lockout = get_object_or_404(LoginLockout, pk=request.POST.get("lockout_id"))
        LoginLockout.objects.filter(pk=lockout.pk).update(failed_attempts=0, locked_until=None)
        messages.success(request, f"Unlocked {lockout.username}.")
        return redirect("security_locked")
    rows = LoginLockout.objects.filter(failed_attempts__gt=0).order_by("-last_failed_at")[:200]
    return render(request, "security/locked.html", {"rows": rows, "now": timezone.now()})


def _can_manage(actor, target):
    """Only an administrator may act on staff or other administrators."""
    return actor.is_superuser or not (target.is_staff or target.is_superuser)


@staff_required
def people(request):
    User = get_user_model()
    if request.method == "POST":
        target = get_object_or_404(User, pk=request.POST.get("user_id"))
        action = request.POST.get("action")
        if target == request.user and action in ("deactivate", "require_change"):
            messages.error(request, "You can't do that to your own account here.")
        elif not _can_manage(request.user, target):
            messages.error(request, "Only an administrator can change a staff account.")
        elif action == "require_change":
            AccountSecurity.objects.update_or_create(user=target, defaults={"must_change_password": True})
            messages.success(request, f"{target.get_username()} must choose a new password at their next page.")
        elif action == "clear_require":
            AccountSecurity.objects.update_or_create(user=target, defaults={"must_change_password": False})
            messages.success(request, f"{target.get_username()} no longer has to change their password.")
        elif action == "sign_out":
            sign_out_everywhere(target)
            messages.success(request, f"Signed {target.get_username()} out of every device.")
        elif action == "unlock":
            unlock_account(target)
            messages.success(request, f"Unlocked {target.get_username()}.")
        elif action == "deactivate":
            target.is_active = False
            target.save(update_fields=["is_active"])
            sign_out_everywhere(target)
            messages.success(request, f"{target.get_username()} was deactivated and signed out.")
        elif action == "activate":
            target.is_active = True
            target.save(update_fields=["is_active"])
            messages.success(request, f"{target.get_username()} was reactivated.")
        return redirect(request.get_full_path())
    q = request.GET.get("q", "").strip()
    users = get_user_model().objects.select_related("security").order_by("username")
    if q:
        users = users.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q))
    return render(request, "security/people.html", {"page": _page(request, users, 30), "q": q})


@staff_required
def visitors(request):
    if request.method == "POST":
        ids = request.POST.getlist("ids")
        action = request.POST.get("action")
        qs = VisitorIP.objects.filter(pk__in=ids)
        if action == "block":
            count, stuck = rules.block_ips(qs.exclude(ip_address=client_ip(request)),
                                           f"Blocked by {request.user.get_username()}")
            messages.success(request, f"Blocked {count} address{'es' if count != 1 else ''}.")
            for ip, net in stuck:
                messages.warning(request, f"{ip} stays unblocked: the whitelisted range {net} covers it. Edit the whitelist in Settings.")
        elif action == "unblock":
            messages.success(request, f"Unblocked {VisitorIP.unblock(qs)} address(es).")
        elif action == "whitelist" and request.user.is_superuser:
            messages.success(request, f"Whitelisted {rules.add_to_whitelist(list(qs.values_list('ip_address', flat=True)))} address(es).")
        return redirect(request.get_full_path())
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "-hit_count")
    if sort.lstrip("-") not in ("hit_count", "last_seen", "first_seen", "ip_address", "country"):
        sort = "-hit_count"
    qs = VisitorIP.objects.all().order_by(sort)
    if q:
        qs = qs.filter(Q(ip_address__startswith=q) | Q(country__iexact=q) | Q(city__icontains=q))
    if request.GET.get("blocked") == "1":
        qs = qs.filter(blocked=True)
    return render(request, "security/visitors.html", {"page": _page(request, qs), "q": q, "sort": sort,
                                                       "blocked_only": request.GET.get("blocked") == "1"})


@admin_required
def settings_page(request):
    form = SecurityConfigForm(request.POST or None, instance=SecurityConfig.get())
    if request.method == "POST" and form.is_valid():
        form.save()
        freed = rules.unblock_whitelisted()
        messages.success(request, "Security settings saved." + (f" {freed} whitelisted address(es) were unblocked." if freed else ""))
        return redirect("security_settings")
    return render(request, "form.html", {"form": form, "title": "Security settings"})


@admin_required
def rules_list(request):
    rows = [(r, rules.describe(r)) for r in SecurityRule.objects.all()]
    return render(request, "security/rules.html", {"rows": rows})


@admin_required
def rule_edit(request, pk=None):
    rule = get_object_or_404(SecurityRule, pk=pk) if pk else None
    form = RuleForm(request.POST or None, rule=rule)
    preview = None
    if request.method == "POST" and form.is_valid():
        fields = form.rule_fields
        if "preview" in request.POST:
            preview = rules.preview(SecurityRule(**fields))
        else:
            if rule is None:
                rule = SecurityRule()
            for k, v in fields.items():
                setattr(rule, k, v)
            rule.save()
            messages.success(request, f"Rule “{rule.name}” saved.")
            return redirect("security_rules")
    return render(request, "security/rule_form.html", {"form": form, "rule": rule, "preview": preview})


@admin_required
@require_POST
def rule_action(request, pk):
    rule = get_object_or_404(SecurityRule, pk=pk)
    if request.POST.get("action") == "delete":
        rule.delete()
        messages.success(request, "Rule deleted.")
    else:
        rule.enabled = not rule.enabled
        rule.save(update_fields=["enabled", "updated_at"])
    return redirect("security_rules")


def _bars(rows, key, label):
    top = max([r[key] for r in rows] or [1]) or 1
    return [{"label": r[label], "n": r[key], "pct": max(2, round(100 * r[key] / top))} for r in rows]


@login_required
def trending(request):
    cfg = SecurityConfig.get()
    if not (request.user.is_superuser or (request.user.is_staff and cfg.trending_for_staff)):
        raise PermissionDenied
    try:
        days = int(request.GET.get("days", 7))
    except ValueError:
        days = 7
    days = days if days in (1, 7, 30, 90) else 7
    since = timezone.now() - timedelta(days=days)
    views = PageView.objects.filter(created_at__gte=since)
    per_day = list(views.annotate(d=TruncDate("created_at")).values("d").annotate(
        views=Count("id"), visitors=Count("visitor_id", distinct=True)).order_by("d"))
    top_pages = list(views.values("path").annotate(n=Count("id")).order_by("-n")[:10])
    referrers = list(views.exclude(referrer="").values("referrer").annotate(n=Count("id")).order_by("-n")[:10])
    devices = list(views.exclude(device_type="").values("device_type").annotate(n=Count("id")).order_by("-n"))
    User = get_user_model()
    signups = list(User.objects.filter(date_joined__gte=since).annotate(d=TruncDate("date_joined"))
                   .values("d").annotate(n=Count("id")).order_by("d"))
    hot = list(Post.objects.filter(created__gte=since).annotate(score=Count("likes", distinct=True) + 2 * Count("comments", distinct=True))
               .filter(score__gt=0).select_related("author__profile").order_by("-score", "-created")[:5])
    return render(request, "security/trending.html", {
        "days": days, "total_views": views.count(), "visitors": views.values("visitor_id").distinct().count(),
        "members_active": views.exclude(user=None).values("user").distinct().count(),
        "new_members": sum(s["n"] for s in signups), "per_day": _bars(per_day, "views", "d"),
        "top_pages": _bars(top_pages, "n", "path"), "referrers": _bars(referrers, "n", "referrer"),
        "devices": _bars(devices, "n", "device_type"), "hot_posts": hot, "tracking_on": cfg.tracking_enabled,
    })
