from django.conf import settings
from django.db import models
from django.db.models import Q

User = settings.AUTH_USER_MODEL


class Connection(models.Model):
    PENDING, ACCEPTED = "pending", "accepted"
    from_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sent_connections")
    to_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="received_connections")
    status = models.CharField(max_length=10, default=PENDING)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("from_user", "to_user")


def connected_ids(user):
    """IDs of users with an accepted connection to `user`."""
    ids = set()
    qs = Connection.objects.filter(status=Connection.ACCEPTED).filter(
        Q(from_user=user) | Q(to_user=user)
    )
    for c in qs:
        ids.add(c.to_user_id if c.from_user_id == user.id else c.from_user_id)
    return ids


def relationship(a, b):
    """One of: self, connected, sent, received, none (from a's point of view)."""
    if a.id == b.id:
        return "self"
    c = Connection.objects.filter(
        Q(from_user=a, to_user=b) | Q(from_user=b, to_user=a)
    ).first()
    if not c:
        return "none"
    if c.status == Connection.ACCEPTED:
        return "connected"
    return "sent" if c.from_user_id == a.id else "received"
