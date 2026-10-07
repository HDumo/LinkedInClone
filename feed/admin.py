from django.contrib import admin

from .models import Comment, Like, Post

for m in (Post, Like, Comment):
    admin.site.register(m)
