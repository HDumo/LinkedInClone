from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL


class Notification(models.Model):
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    verb = models.CharField(max_length=200)
    url = models.CharField(max_length=200, blank=True)
    system = models.BooleanField(default=False, help_text="A message from the site itself, not from another member.")
    read = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]


def notify(recipient, actor, verb, url=""):
    """Create a notification unless a user is notifying themselves."""
    if recipient.id != actor.id:
        Notification.objects.create(recipient=recipient, actor=actor, verb=verb, url=url)


def notify_system(recipient, text, url=""):
    """A notice from the site itself (security alerts, etc.). The recipient is
    recorded as the actor only to satisfy the column; templates show `text`."""
    return Notification.objects.create(recipient=recipient, actor=recipient, verb=text[:200], url=url, system=True)


def notify_admins(text, url=""):
    from django.contrib.auth import get_user_model

    for admin in get_user_model().objects.filter(is_superuser=True, is_active=True):
        notify_system(admin, text, url)
