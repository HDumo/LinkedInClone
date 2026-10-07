from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    verb = models.CharField(max_length=200)
    url = models.CharField(max_length=200, blank=True)
    read = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]


def notify(recipient, actor, verb, url=""):
    """Create a notification unless a user is notifying themselves."""
    if recipient.id != actor.id:
        Notification.objects.create(recipient=recipient, actor=actor, verb=verb, url=url)
