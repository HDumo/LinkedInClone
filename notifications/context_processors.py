def unread(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from messaging.models import Message

    return {
        "unread_notifications": user.notifications.filter(read=False).count(),
        "unread_messages": Message.objects.filter(recipient=user, read=False).count(),
    }
