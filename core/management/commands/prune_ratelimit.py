from django.core.management.base import BaseCommand

from core.ratecache import prune


class Command(BaseCommand):
    help = "Delete expired rate-limit counters (safe to run from cron or a systemd timer)."

    def handle(self, *args, **opts):
        self.stdout.write(f"Removed {prune()} expired rate-limit counter(s).")
