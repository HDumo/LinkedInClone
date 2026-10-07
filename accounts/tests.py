from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Endorsement, Profile, Skill


def make_user(name, **kw):
    kw.setdefault("email", f"{name}@example.com")
    return User.objects.create_user(name, password="pw-12345-xyz", first_name=name.title(), **kw)


class SignupLoginTests(TestCase):
    def test_signup_creates_user_and_profile_and_logs_in(self):
        r = self.client.post(reverse("signup"), {
            "username": "newbie", "first_name": "New", "last_name": "Bie", "email": "n@example.com",
            "password1": "a-strong-pass-99", "password2": "a-strong-pass-99",
        })
        self.assertRedirects(r, reverse("profile_edit"))
        self.assertTrue(Profile.objects.filter(user__username="newbie").exists())
        self.assertEqual(self.client.get(reverse("home")).status_code, 200)

    def test_signup_rejects_mismatched_passwords(self):
        r = self.client.post(reverse("signup"), {
            "username": "x", "first_name": "x", "last_name": "x", "email": "x@example.com",
            "password1": "a-strong-pass-99", "password2": "different-pass-99",
        })
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(username="x").exists())

    def test_login_and_logout(self):
        make_user("al")
        self.assertTrue(self.client.login(username="al", password="pw-12345-xyz"))
        self.client.post(reverse("logout"))
        self.assertRedirects(self.client.get(reverse("home")), f"{reverse('login')}?next=/")

    def test_anonymous_redirected_from_private_pages(self):
        for name in ("home", "my_network", "people", "inbox", "job_list", "notifications", "profile_edit"):
            r = self.client.get(reverse(name))
            self.assertEqual(r.status_code, 302, name)
            self.assertIn(reverse("login"), r["Location"])


class ProfileTests(TestCase):
    def setUp(self):
        self.a, self.b = make_user("alice"), make_user("bob")

    def test_profile_is_public_to_logged_in_users(self):
        self.client.force_login(self.b)
        self.assertContains(self.client.get(reverse("profile", args=["alice"])), "Alice")

    def test_unknown_profile_404(self):
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(reverse("profile", args=["nobody"])).status_code, 404)

    def test_edit_profile(self):
        self.client.force_login(self.a)
        self.client.post(reverse("profile_edit"), {"headline": "Engineer", "location": "Denver", "about": "Hi"})
        self.a.profile.refresh_from_db()
        self.assertEqual(self.a.profile.headline, "Engineer")

    def test_add_experience_education_skill(self):
        self.client.force_login(self.a)
        self.client.post(reverse("add_experience"), {"title": "Dev", "company": "Acme", "start_date": "2020-01-01"})
        self.client.post(reverse("add_education"), {"school": "CU", "degree": "BS", "start_year": 2012, "end_year": 2016})
        self.client.post(reverse("add_skill"), {"name": "Python"})
        p = self.a.profile
        self.assertEqual((p.experiences.count(), p.educations.count(), p.skills.count()), (1, 1, 1))

    def test_experience_end_before_start_rejected(self):
        self.client.force_login(self.a)
        r = self.client.post(reverse("add_experience"), {
            "title": "Dev", "company": "Acme", "start_date": "2020-05-01", "end_date": "2019-01-01"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.a.profile.experiences.count(), 0)

    def test_duplicate_skill_does_not_crash(self):
        Skill.objects.create(profile=self.a.profile, name="Python")
        self.client.force_login(self.a)
        r = self.client.post(reverse("add_skill"), {"name": "Python"}, follow=True)
        self.assertContains(r, "already exists")
        self.assertEqual(self.a.profile.skills.count(), 1)

    def test_endorse_once_not_self_and_notifies(self):
        skill = Skill.objects.create(profile=self.a.profile, name="Python")
        self.client.force_login(self.b)
        self.client.post(reverse("endorse", args=[skill.id]))
        self.client.post(reverse("endorse", args=[skill.id]))
        self.assertEqual(Endorsement.objects.count(), 1)
        self.assertEqual(self.a.notifications.count(), 1)
        self.client.force_login(self.a)
        self.client.post(reverse("endorse", args=[skill.id]))
        self.assertEqual(Endorsement.objects.count(), 1)

    def test_endorse_requires_post(self):
        skill = Skill.objects.create(profile=self.a.profile, name="Python")
        self.client.force_login(self.b)
        self.assertEqual(self.client.get(reverse("endorse", args=[skill.id])).status_code, 405)
