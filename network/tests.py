from django.test import TestCase
from django.urls import reverse

from accounts.tests import make_user

from .models import Connection, connected_ids, relationship


class ConnectionTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = make_user("alice"), make_user("bob"), make_user("carol")

    def test_request_accept_flow(self):
        self.client.force_login(self.a)
        self.client.post(reverse("connect", args=[self.b.id]))
        self.assertEqual(relationship(self.a, self.b), "sent")
        self.assertEqual(relationship(self.b, self.a), "received")
        self.assertEqual(self.b.notifications.count(), 1)

        self.client.force_login(self.b)
        conn = Connection.objects.get()
        self.client.post(reverse("respond", args=[conn.id]), {"action": "accept"})
        self.assertEqual(relationship(self.a, self.b), "connected")
        self.assertEqual(connected_ids(self.a), {self.b.id})
        self.assertEqual(self.a.notifications.count(), 1)

    def test_decline_removes_request(self):
        Connection.objects.create(from_user=self.a, to_user=self.b)
        self.client.force_login(self.b)
        self.client.post(reverse("respond", args=[Connection.objects.get().id]), {"action": "decline"})
        self.assertEqual(relationship(self.a, self.b), "none")

    def test_cannot_connect_with_self_or_duplicate(self):
        self.client.force_login(self.a)
        self.client.post(reverse("connect", args=[self.a.id]))
        self.assertEqual(Connection.objects.count(), 0)
        self.client.post(reverse("connect", args=[self.b.id]))
        self.client.post(reverse("connect", args=[self.b.id]))
        self.assertEqual(Connection.objects.count(), 1)

    def test_mutual_request_auto_accepts(self):
        Connection.objects.create(from_user=self.a, to_user=self.b)
        self.client.force_login(self.b)
        self.client.post(reverse("connect", args=[self.a.id]))
        self.assertEqual(Connection.objects.count(), 1)
        self.assertEqual(relationship(self.a, self.b), "connected")

    def test_only_recipient_can_respond(self):
        Connection.objects.create(from_user=self.a, to_user=self.b)
        self.client.force_login(self.c)
        r = self.client.post(reverse("respond", args=[Connection.objects.get().id]), {"action": "accept"})
        self.assertEqual(r.status_code, 404)
        self.client.force_login(self.a)  # sender cannot self-accept either
        r = self.client.post(reverse("respond", args=[Connection.objects.get().id]), {"action": "accept"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(Connection.objects.get().status, Connection.PENDING)

    def test_remove_connection(self):
        Connection.objects.create(from_user=self.a, to_user=self.b, status=Connection.ACCEPTED)
        self.client.force_login(self.b)
        self.client.post(reverse("remove_connection", args=[self.a.id]))
        self.assertEqual(Connection.objects.count(), 0)

    def test_connect_requires_post(self):
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(reverse("connect", args=[self.b.id])).status_code, 405)

    def test_people_search_and_global_search(self):
        self.client.force_login(self.a)
        self.assertContains(self.client.get(reverse("people"), {"q": "bob"}), "Bob")
        self.assertNotContains(self.client.get(reverse("people"), {"q": "bob"}), "Carol")
        self.assertContains(self.client.get(reverse("search"), {"q": "carol"}), "Carol")
        self.assertEqual(self.client.get(reverse("search")).status_code, 200)
