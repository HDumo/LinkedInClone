from django.test import TestCase
from django.urls import reverse

from accounts.tests import make_user
from network.models import Connection

from .models import Comment, Like, Post


class FeedTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c = make_user("alice"), make_user("bob"), make_user("carol")
        Connection.objects.create(from_user=self.a, to_user=self.b, status=Connection.ACCEPTED)

    def test_create_post_shows_in_own_and_connection_feed_not_strangers(self):
        self.client.force_login(self.a)
        self.client.post(reverse("create_post"), {"body": "hello world"})
        self.assertContains(self.client.get(reverse("home")), "hello world")
        self.client.force_login(self.b)
        self.assertContains(self.client.get(reverse("home")), "hello world")
        self.client.force_login(self.c)
        self.assertNotContains(self.client.get(reverse("home")), "hello world")

    def test_empty_post_rejected(self):
        self.client.force_login(self.a)
        self.client.post(reverse("create_post"), {"body": "   "})
        self.assertEqual(Post.objects.count(), 0)

    def test_html_is_escaped(self):
        self.client.force_login(self.a)
        self.client.post(reverse("create_post"), {"body": "<script>alert(1)</script>"})
        r = self.client.get(reverse("home"))
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertContains(r, "&lt;script&gt;")

    def test_like_toggle_and_notification(self):
        post = Post.objects.create(author=self.a, body="x")
        self.client.force_login(self.b)
        self.client.post(reverse("toggle_like", args=[post.id]))
        self.assertEqual(Like.objects.count(), 1)
        self.assertEqual(self.a.notifications.count(), 1)
        self.client.post(reverse("toggle_like", args=[post.id]))
        self.assertEqual(Like.objects.count(), 0)

    def test_comment(self):
        post = Post.objects.create(author=self.a, body="x")
        self.client.force_login(self.b)
        self.client.post(reverse("add_comment", args=[post.id]), {"body": "nice"})
        self.client.post(reverse("add_comment", args=[post.id]), {"body": "  "})
        self.assertEqual(Comment.objects.count(), 1)

    def test_stranger_cannot_like_comment_or_share_invisible_post(self):
        post = Post.objects.create(author=self.a, body="x")
        self.client.force_login(self.c)
        for name in ("toggle_like", "add_comment", "share_post"):
            self.assertEqual(self.client.post(reverse(name, args=[post.id]), {"body": "y"}).status_code, 404, name)

    def test_only_author_can_delete(self):
        post = Post.objects.create(author=self.a, body="x")
        self.client.force_login(self.b)
        self.assertEqual(self.client.post(reverse("delete_post", args=[post.id])).status_code, 404)
        self.assertEqual(Post.objects.count(), 1)
        self.client.force_login(self.a)
        self.client.post(reverse("delete_post", args=[post.id]))
        self.assertEqual(Post.objects.count(), 0)

    def test_share_points_to_original(self):
        post = Post.objects.create(author=self.a, body="original")
        self.client.force_login(self.b)
        self.client.post(reverse("share_post", args=[post.id]), {"body": "look"})
        share = Post.objects.get(author=self.b)
        self.assertEqual(share.shared_from, post)
        self.assertContains(self.client.get(reverse("home")), "original")
