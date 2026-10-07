from django import forms
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from network.models import connected_ids
from notifications.models import notify

from .models import Comment, Like, Post


class PostForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ("body",)


def _visible_posts(user):
    return Post.objects.filter(author_id__in=connected_ids(user) | {user.id})


@login_required
def home(request):
    posts = (
        _visible_posts(request.user)
        .select_related("author__profile", "shared_from__author")
        .prefetch_related("likes", "comments__user")
    )
    liked = set(Like.objects.filter(user=request.user, post__in=posts).values_list("post_id", flat=True))
    return render(request, "feed/home.html", {"posts": posts[:50], "liked": liked, "form": PostForm()})


@login_required
@require_POST
def create_post(request):
    form = PostForm(request.POST)
    if form.is_valid():
        post = form.save(commit=False)
        post.author = request.user
        post.save()
    return redirect("home")


@login_required
@require_POST
def delete_post(request, pk):
    get_object_or_404(Post, pk=pk, author=request.user).delete()
    return redirect("home")


def _get_visible(request, pk):
    return get_object_or_404(_visible_posts(request.user), pk=pk)


@login_required
@require_POST
def toggle_like(request, pk):
    post = _get_visible(request, pk)
    like, created = Like.objects.get_or_create(post=post, user=request.user)
    if created:
        notify(post.author, request.user, "liked your post", reverse("home"))
    else:
        like.delete()
    return redirect("home")


@login_required
@require_POST
def add_comment(request, pk):
    post = _get_visible(request, pk)
    body = request.POST.get("body", "").strip()[:1000]
    if body:
        Comment.objects.create(post=post, user=request.user, body=body)
        notify(post.author, request.user, "commented on your post", reverse("home"))
    return redirect("home")


@login_required
@require_POST
def share_post(request, pk):
    original = _get_visible(request, pk)
    original = original.shared_from or original
    Post.objects.create(author=request.user, body=request.POST.get("body", "").strip(), shared_from=original)
    notify(original.author, request.user, "shared your post", reverse("home"))
    return redirect("home")
