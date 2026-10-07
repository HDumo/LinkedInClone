from django.test import TestCase
from django.urls import reverse

from accounts.tests import make_user

from .models import Application, Company, Job


class JobTests(TestCase):
    def setUp(self):
        self.poster, self.seeker, self.other = make_user("poster"), make_user("seeker"), make_user("other")
        self.company = Company.objects.create(name="Acme Corp", owner=self.poster)
        self.job = Job.objects.create(title="Backend Dev", company=self.company, posted_by=self.poster, location="Remote", description="Python")

    def test_company_slug_unique(self):
        c2 = Company.objects.create(name="Acme Corp!", owner=self.seeker)
        self.assertNotEqual(c2.slug, self.company.slug)

    def test_create_company_and_job_only_with_own_company(self):
        self.client.force_login(self.seeker)
        r = self.client.post(reverse("job_create"), {"title": "X", "company": self.company.id, "location": "", "description": "d"})
        self.assertEqual(r.status_code, 200)  # not their company: form invalid
        self.assertEqual(Job.objects.count(), 1)
        self.client.post(reverse("company_create"), {"name": "Mine", "description": "", "website": ""})
        mine = Company.objects.get(name="Mine")
        self.client.post(reverse("job_create"), {"title": "X", "company": mine.id, "location": "", "description": "d"})
        self.assertEqual(Job.objects.count(), 2)

    def test_search(self):
        self.client.force_login(self.seeker)
        self.assertContains(self.client.get(reverse("job_list"), {"q": "backend"}), "Backend Dev")
        self.assertNotContains(self.client.get(reverse("job_list"), {"q": "zzz"}), "Backend Dev")

    def test_apply_once_and_notify(self):
        self.client.force_login(self.seeker)
        self.client.post(reverse("job_apply", args=[self.job.id]), {"cover_letter": "pick me"})
        self.client.post(reverse("job_apply", args=[self.job.id]), {"cover_letter": "again"})
        self.assertEqual(Application.objects.count(), 1)
        self.assertEqual(self.poster.notifications.count(), 1)
        self.assertContains(self.client.get(reverse("my_applications")), "Backend Dev")

    def test_cannot_apply_to_own_job(self):
        self.client.force_login(self.poster)
        self.assertEqual(self.client.post(reverse("job_apply", args=[self.job.id])).status_code, 403)

    def test_only_poster_sees_applicants(self):
        Application.objects.create(job=self.job, applicant=self.seeker, cover_letter="secret letter")
        self.client.force_login(self.poster)
        self.assertContains(self.client.get(reverse("job_detail", args=[self.job.id])), "secret letter")
        for u in (self.seeker, self.other):
            self.client.force_login(u)
            self.assertNotContains(self.client.get(reverse("job_detail", args=[self.job.id])), "secret letter")

    def test_company_page_lists_jobs(self):
        self.client.force_login(self.other)
        self.assertContains(self.client.get(reverse("company_detail", args=[self.company.slug])), "Backend Dev")
