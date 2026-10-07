from django import forms
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from network.models import connected_ids
from notifications.models import notify

from .models import Message


class MessageForm(forms.ModelForm):
    class Meta:
        model = Message
        fields = ("body",)


@login_required
def inbox(request):
    me = request.user
    msgs = Message.objects.filter(Q(sender=me) | Q(recipient=me)).select_related("sender", "recipient").order_by("-created", "-id")
    seen, threads = set(), []
    for m in msgs:
        other = m.recipient if m.sender_id == me.id else m.sender
        if other.id in seen:
            continue
        seen.add(other.id)
        unread = Message.objects.filter(sender=other, recipient=me, read=False).count()
        threads.append((other, m, unread))
    friends = User.objects.filter(id__in=connected_ids(me) - seen).select_related("profile")
    return render(request, "messaging/inbox.html", {"threads": threads, "friends": friends})


@login_required
@require_http_methods(["GET", "POST"])
def thread(request, user_id):
    other = get_object_or_404(User, pk=user_id)
    if other.id == request.user.id or other.id not in connected_ids(request.user):
        return HttpResponseForbidden("You can only message your connections.")
    if request.method == "POST":
        form = MessageForm(request.POST)
        if form.is_valid():
            msg = form.save(commit=False)
            msg.sender, msg.recipient = request.user, other
            msg.save()
            notify(other, request.user, "sent you a message", reverse("thread", args=[request.user.id]))
        return redirect("thread", user_id=other.id)
    Message.objects.filter(sender=other, recipient=request.user, read=False).update(read=True)
    msgs = Message.objects.filter(
        Q(sender=request.user, recipient=other) | Q(sender=other, recipient=request.user)
    )
    return render(request, "messaging/thread.html", {"other": other, "messages_list": msgs, "form": MessageForm()})
