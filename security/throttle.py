"""Escalating waits for repeated tries, shared by every server process.

After FREE_TRIES tries, each further try makes the person wait the next step
of WAITS: 1 minute, then 2, 3, 5, 10, 30 and 60 minutes (and 60 from then
on). A day with no tries starts the count over. A success clears it. Counts
live in the database, not in each process's memory, so landing on another
server process can't reset them."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

FREE_TRIES = 4
WAITS = [60, 120, 180, 300, 600, 1800, 3600]  # seconds
FORGET_AFTER = timedelta(days=1)


def _normalize(key):
    return key.strip().lower()[:200]


def wait_seconds(*keys):
    """Seconds until any of these keys may try again (0 = go ahead)."""
    from .models import AttemptThrottle

    now = timezone.now()
    until = (AttemptThrottle.objects.filter(key__in=[_normalize(k) for k in keys], blocked_until__gt=now)
             .order_by("-blocked_until").values_list("blocked_until", flat=True).first())
    return int((until - now).total_seconds()) + 1 if until else 0


def record_try(*keys):
    """Counts one try against each key. Returns the wait (seconds) that the
    try just started, 0 if still within the free tries."""
    from .models import AttemptThrottle

    now = timezone.now()
    longest = 0
    with transaction.atomic():
        for key in dict.fromkeys(_normalize(k) for k in keys):
            row, _ = AttemptThrottle.objects.select_for_update().get_or_create(key=key)
            if now - row.last_attempt_at > FORGET_AFTER:
                row.attempts = 0
            row.attempts += 1
            row.last_attempt_at = now
            if row.attempts >= FREE_TRIES:
                wait = WAITS[min(row.attempts - FREE_TRIES, len(WAITS) - 1)]
                row.blocked_until = now + timedelta(seconds=wait)
                longest = max(longest, wait)
            row.save()
    return longest


def clear(*keys):
    from .models import AttemptThrottle

    AttemptThrottle.objects.filter(key__in=[_normalize(k) for k in keys]).delete()


def prune():
    from .models import AttemptThrottle

    return AttemptThrottle.objects.filter(last_attempt_at__lt=timezone.now() - FORGET_AFTER).delete()[0]
