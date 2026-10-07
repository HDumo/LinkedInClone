"""A small cache backend used only for request limits ("10 per minute").

Why not Django's default cache: it lives inside each server process, and
production runs several, so each kept its own count and a visitor who was
told "too many requests" often got through on the next try. Django's
database cache is shared but its incr() isn't atomic. This stores counts
in the RateCounter table and adds to them with a single UPDATE, so counts
are exact and shared. Expired rows are removed by `manage.py prune_ratelimit`.
"""
from datetime import timedelta

from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone


class DatabaseCounterCache(BaseCache):
    """Holds whole-number counts only (that's all django-ratelimit stores)."""

    def __init__(self, location, params):
        super().__init__(params)

    def _expires(self, timeout):
        if timeout is DEFAULT_TIMEOUT:
            timeout = self.default_timeout
        if timeout is None:
            return timezone.now() + timedelta(days=3650)
        return timezone.now() + timedelta(seconds=max(0, timeout))

    def _live(self, key, version=None):
        from .models import RateCounter

        key = self.make_and_validate_key(key, version=version)
        return key, RateCounter.objects.filter(key=key, expires__gt=timezone.now())

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        from .models import RateCounter

        key = self.make_and_validate_key(key, version=version)
        RateCounter.objects.filter(key=key, expires__lte=timezone.now()).delete()
        try:
            with transaction.atomic():
                RateCounter.objects.create(key=key, count=int(value), expires=self._expires(timeout))
        except IntegrityError:  # someone else holds a live count for this key
            return False
        return True

    def get(self, key, default=None, version=None):
        _, rows = self._live(key, version)
        count = rows.values_list("count", flat=True).first()
        return default if count is None else count

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        from .models import RateCounter

        key = self.make_and_validate_key(key, version=version)
        RateCounter.objects.update_or_create(
            key=key, defaults={"count": int(value), "expires": self._expires(timeout)}
        )

    def incr(self, key, delta=1, version=None):
        _, rows = self._live(key, version)
        if not rows.update(count=F("count") + delta):  # one atomic UPDATE
            raise ValueError(f"Key '{key}' not found")
        return rows.values_list("count", flat=True).first()

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        _, rows = self._live(key, version)
        return bool(rows.update(expires=self._expires(timeout)))

    def delete(self, key, version=None):
        from .models import RateCounter

        key = self.make_and_validate_key(key, version=version)
        return bool(RateCounter.objects.filter(key=key).delete()[0])

    def has_key(self, key, version=None):
        return self._live(key, version)[1].exists()

    def clear(self):
        from .models import RateCounter

        RateCounter.objects.all().delete()


def prune():
    from .models import RateCounter

    return RateCounter.objects.filter(expires__lte=timezone.now()).delete()[0]
