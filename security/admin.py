from django.contrib import admin

from . import models

for m in (models.SecurityConfig, models.LoginEvent, models.LoginLockout, models.AccountSecurity, models.UserSession,
          models.VisitorIP, models.SecurityRule, models.PageView):
    admin.site.register(m)
