from django.contrib import admin

from .models import Application, Company, Job

for m in (Company, Job, Application):
    admin.site.register(m)
