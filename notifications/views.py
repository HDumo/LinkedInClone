from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def notification_list(request):
    items = list(request.user.notifications.select_related("actor")[:50])
    unread_ids = [n.id for n in items if not n.read]
    response = render(request, "notifications/list.html", {"items": items, "unread_ids": set(unread_ids)})
    request.user.notifications.filter(id__in=unread_ids).update(read=True)
    return response
