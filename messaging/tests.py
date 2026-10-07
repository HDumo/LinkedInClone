from django.test import TestCase
from django.urls import reverse

from accounts.tests import make_user
from network.models import Connection

from .models import Message


class MessagingTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = make_user("alice"), make_user("bob"), make_user("carol")
        Connection.objects.create(from_user=self.a, to_user=self.b, status=Connection.ACCEPTED)

    def test_connected_users_can_chat_and_unread_clears(self):
        self.client.force_login(self.a)
        self.client.post(reverse("thread", args=[self.b.id]), {"body": "hi bob"})
        self.assertEqual(self.b.notifications.count(), 1)
        self.client.force_login(self.b)
        self.assertContains(self.client.get(reverse("inbox")), "hi bob")
        self.assertEqual(Message.objects.filter(read=False).count(), 1)
        self.assertContains(self.client.get(reverse("thread", args=[self.a.id])), "hi bob")
        self.assertEqual(Message.objects.filter(read=False).count(), 0)

    def test_cannot_message_non_connection_or_self(self):
        self.client.force_login(self.a)
        self.assertEqual(self.client.get(reverse("thread", args=[self.c.id])).status_code, 403)
        self.assertEqual(self.client.post(reverse("thread", args=[self.c.id]), {"body": "x"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("thread", args=[self.a.id])).status_code, 403)
        self.assertEqual(Message.objects.count(), 0)

    def test_third_party_cannot_read_conversation(self):
        Message.objects.create(sender=self.a, recipient=self.b, body="secret")
        self.client.force_login(self.c)
        self.assertNotContains(self.client.get(reverse("inbox")), "secret")
        self.assertEqual(self.client.get(reverse("thread", args=[self.a.id])).status_code, 403)

    def test_empty_message_rejected(self):
        self.client.force_login(self.a)
        self.client.post(reverse("thread", args=[self.b.id]), {"body": ""})
        self.assertEqual(Message.objects.count(), 0)
