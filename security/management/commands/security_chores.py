from django.core.management.base import BaseCommand

from security.events import prune_all


class Command(BaseCommand):
    help = "Daily housekeeping: delete old login history, expired codes, counters and page views."

    def handle(self, *args, **opts):
        for what, n in prune_all().items():
            self.stdout.write(f"Removed {n} old {what}.")
