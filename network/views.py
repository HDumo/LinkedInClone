from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from jobs.models import Company, Job
from notifications.models import notify

from .models import Connection, connected_ids, relationship


@login_required
def my_network(request):
    ids = connected_ids(request.user)
    return render(request, "network/network.html", {
        "connections": User.objects.filter(id__in=ids).select_related("profile"),
        "invites": Connection.objects.filter(to_user=request.user, status=Connection.PENDING).select_related("from_user__profile"),
    })


@login_required
def people(request):
    q = request.GET.get("q", "").strip()
    users = User.objects.exclude(id=request.user.id).select_related("profile")
    if q:
        users = users.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(profile__headline__icontains=q)
        )
    rows = [(u, relationship(request.user, u)) for u in users[:50]]
    return render(request, "network/people.html", {"rows": rows, "q": q})


@login_required
@require_POST
def connect(request, user_id):
    other = get_object_or_404(User, pk=user_id)
    if other == request.user:
        messages.error(request, "You cannot connect with yourself.")
        return redirect("people")
    rel = relationship(request.user, other)
    if rel == "received":  # they already asked us: accept
        conn = Connection.objects.get(from_user=other, to_user=request.user)
        conn.status = Connection.ACCEPTED
        conn.save()
        notify(other, request.user, "accepted your connection request", reverse("profile", args=[request.user.username]))
    elif rel == "none":
        Connection.objects.create(from_user=request.user, to_user=other)
        notify(other, request.user, "sent you a connection request", reverse("my_network"))
    return redirect(request.POST.get("next") or "people")


@login_required
@require_POST
def respond(request, pk):
    conn = get_object_or_404(Connection, pk=pk, to_user=request.user, status=Connection.PENDING)
    if request.POST.get("action") == "accept":
        conn.status = Connection.ACCEPTED
        conn.save()
        notify(conn.from_user, request.user, "accepted your connection request", reverse("profile", args=[request.user.username]))
    else:
        conn.delete()
    return redirect("my_network")


@login_required
@require_POST
def remove(request, user_id):
    other = get_object_or_404(User, pk=user_id)
    Connection.objects.filter(
        Q(from_user=request.user, to_user=other) | Q(from_user=other, to_user=request.user)
    ).delete()
    return redirect("my_network")


@login_required
def search(request):
    q = request.GET.get("q", "").strip()
    ctx = {"q": q, "users": [], "jobs": [], "companies": []}
    if q:
        ctx["users"] = User.objects.filter(
            Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(profile__headline__icontains=q)
        ).select_related("profile")[:20]
        ctx["jobs"] = Job.objects.filter(Q(title__icontains=q) | Q(description__icontains=q))[:20]
        ctx["companies"] = Company.objects.filter(name__icontains=q)[:20]
    return render(request, "network/search.html", ctx)
