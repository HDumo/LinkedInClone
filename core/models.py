from django.db import models


class RateCounter(models.Model):
    """One live request count per rate-limit key, shared by every server
    process; see core/ratecache.py."""

    key = models.CharField(max_length=250, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires = models.DateTimeField(db_index=True)
