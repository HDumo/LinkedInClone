from django.test import TestCase
from django.urls import reverse

from accounts.tests import make_user

from .models import notify


class NotificationTests(TestCase):
    def test_no_self_notifications(self):
        a = make_user("alice")
        notify(a, a, "did a thing")
        self.assertEqual(a.notifications.count(), 0)

    def test_badge_then_marked_read_after_viewing(self):
        a, b = make_user("alice"), make_user("bob")
        notify(a, b, "poked you", "/")
        self.client.force_login(a)
        self.assertEqual(self.client.get(reverse("home")).context["unread_notifications"], 1)
        self.assertContains(self.client.get(reverse("notifications")), "poked you")
        self.assertEqual(self.client.get(reverse("home")).context["unread_notifications"], 0)

    def test_users_only_see_their_own(self):
        a, b = make_user("alice"), make_user("bob")
        notify(a, b, "private thing", "/")
        self.client.force_login(b)
        self.assertNotContains(self.client.get(reverse("notifications")), "private thing")
