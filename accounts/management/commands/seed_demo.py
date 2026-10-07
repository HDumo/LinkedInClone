from datetime import date

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from accounts.models import Experience, Skill
from feed.models import Post
from jobs.models import Company, Job
from network.models import Connection

PEOPLE = [
    ("alice", "Alice", "Nguyen", "Staff Engineer at Acme", "Denver, CO"),
    ("bob", "Bob", "Martinez", "Recruiter at Acme", "Austin, TX"),
    ("carol", "Carol", "Singh", "Product Designer", "Remote"),
    ("dave", "Dave", "Okafor", "Data Analyst", "Chicago, IL"),
]


class Command(BaseCommand):
    help = "Create demo users (password: demo-pass-123), connections, posts and a job."

    def handle(self, *args, **opts):
        users = {}
        for username, first, last, headline, location in PEOPLE:
            u, created = User.objects.get_or_create(
                username=username, defaults={"first_name": first, "last_name": last, "email": f"{username}@example.com"}
            )
            if created:
                u.set_password("demo-pass-123")
                u.save()
            u.profile.headline, u.profile.location = headline, location
            u.profile.save()
            users[username] = u
        Experience.objects.get_or_create(profile=users["alice"].profile, title="Staff Engineer", company="Acme", start_date=date(2020, 1, 1))
        Skill.objects.get_or_create(profile=users["alice"].profile, name="Python")
        for a, b in [("alice", "bob"), ("alice", "carol")]:
            Connection.objects.get_or_create(from_user=users[a], to_user=users[b], defaults={"status": Connection.ACCEPTED})
        Connection.objects.get_or_create(from_user=users["dave"], to_user=users["alice"])
        if not Post.objects.exists():
            Post.objects.create(author=users["alice"], body="Excited to join the LinkedClone beta!")
            Post.objects.create(author=users["bob"], body="We are hiring engineers at Acme.")
        acme, _ = Company.objects.get_or_create(name="Acme", defaults={"owner": users["bob"], "description": "Anvils and more."})
        Job.objects.get_or_create(title="Backend Engineer", defaults={"company": acme, "posted_by": users["bob"], "location": "Remote", "description": "Build services in Python."})
        self.stdout.write(self.style.SUCCESS("Seeded demo data. Log in as alice / demo-pass-123."))
