"""Download (or refresh) the free DB-IP "IP to City Lite" database used for
country-based security rules and for showing where a sign-in came from.
DB-IP publishes a new file each month. IP geolocation by DB-IP
(https://db-ip.com), licensed CC BY 4.0."""
import gzip
import os
import shutil
import tempfile
from datetime import date

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"


class Command(BaseCommand):
    help = "Download the free DB-IP City Lite database (IP address locations)."

    def handle(self, *args, **options):
        target = str(settings.IP_LOCATION_DB)
        folder = os.path.dirname(target)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            raise CommandError(f"Can't create {folder}: {e}. Run this command with sudo.")
        if not os.access(folder, os.W_OK):
            raise CommandError(f"Can't write to {folder}. Run it with sudo, e.g. "
                               f"sudo /opt/linkedclone/deploy-release.sh manage update_ip_location_db")
        self.stdout.write(f"Saving to {target} (set IP_LOCATION_DB in .env to change this)")
        today = date.today()
        prev = date(today.year - (today.month == 1), (today.month - 2) % 12 + 1, 1)
        last_error = None
        for month in (today.strftime("%Y-%m"), prev.strftime("%Y-%m")):
            url = URL.format(month=month)
            try:
                with requests.get(url, stream=True, timeout=60) as r:
                    if r.status_code == 404:
                        last_error = f"{url}: not published yet"
                        continue
                    r.raise_for_status()
                    with tempfile.NamedTemporaryFile(dir=folder, delete=False) as tmp:
                        with gzip.GzipFile(fileobj=r.raw) as gz:
                            shutil.copyfileobj(gz, tmp)
                import maxminddb

                maxminddb.open_database(tmp.name).close()  # refuse a corrupt download
                os.replace(tmp.name, target)
                os.chmod(target, 0o644)
                from security.iplocate import reset_cache

                reset_cache()
                self.stdout.write(self.style.SUCCESS(f"Installed {month} database at {target}"))
                return
            except Exception as e:  # try last month's file before giving up
                last_error = f"{url}: {e}"
        raise CommandError(f"Could not download the database. Last problem: {last_error}")
