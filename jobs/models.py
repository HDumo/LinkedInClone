from django.conf import settings
from django.db import models
from django.utils.text import slugify

User = settings.AUTH_USER_MODEL


class Company(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(unique=True, blank=True)
    description = models.TextField(blank=True)
    website = models.URLField(blank=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="companies")

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name) or "company"
            slug, n = base, 2
            while Company.objects.filter(slug=slug).exists():
                slug, n = f"{base}-{n}", n + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Job(models.Model):
    title = models.CharField(max_length=140)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="jobs")
    posted_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="jobs")
    location = models.CharField(max_length=100, blank=True)
    description = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created", "-id"]


class Application(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    applicant = models.ForeignKey(User, on_delete=models.CASCADE, related_name="applications")
    cover_letter = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("job", "applicant")
        ordering = ["-created"]
