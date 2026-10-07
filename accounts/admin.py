from django.contrib import admin

from .models import Education, Endorsement, Experience, Profile, Skill

for m in (Profile, Experience, Education, Skill, Endorsement):
    admin.site.register(m)
