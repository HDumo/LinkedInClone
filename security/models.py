from django.conf import settings
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class SecurityConfig(models.Model):
    """Site-wide security settings (Security → Settings). One row."""

    REUSE_ALLOW, REUSE_LAST, REUSE_FIVE = 0, 1, 5
    REUSE_CHOICES = [
        (REUSE_ALLOW, "Allow reusing old passwords"),
        (REUSE_LAST, "Block the current password"),
        (REUSE_FIVE, "Block the 5 most recent passwords"),
    ]
    login_history_days = models.PositiveIntegerField(
        default=365, help_text="Login records older than this are deleted automatically (0 = keep forever).")
    password_reuse_block = models.PositiveSmallIntegerField(default=REUSE_LAST, choices=REUSE_CHOICES)
    whitelist = models.TextField(
        blank=True, help_text="IP addresses or ranges (like 203.0.113.0/24) that are never blocked, one per line; "
                              "anything after # is a note.")
    tracking_enabled = models.BooleanField(default=True, help_text="Record page visits for Trending.")
    tracking_days = models.PositiveIntegerField(default=90, help_text="Visit records older than this many days are deleted.")
    trending_for_staff = models.BooleanField(
        default=False, help_text="Let every staff member see Trending (by default only administrators can).")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = "Security settings"

    def __str__(self):
        return "Security settings"

    @classmethod
    def get(cls):
        return cls.objects.order_by("pk").first() or cls.objects.create()


class LoginEvent(models.Model):
    """One sign-in attempt, successful or not. Pruned after login_history_days."""

    OK, BAD_PASSWORD, LOCKED, UNKNOWN_USER, INACTIVE, OTP_UNLOCK = "ok", "bad_password", "locked", "unknown_user", "inactive", "otp_unlock"
    RESULT_CHOICES = [
        (OK, "Signed in"), (BAD_PASSWORD, "Wrong password"), (LOCKED, "Blocked: account locked"),
        (UNKNOWN_USER, "No such account"), (INACTIVE, "Account deactivated"), (OTP_UNLOCK, "Unlocked with a code"),
    ]
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="login_events")
    username = models.CharField(max_length=150, help_text="What was typed.")
    result = models.CharField(max_length=14, choices=RESULT_CHOICES)
    page = models.CharField(max_length=12, blank=True, help_text="member or admin login page.")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    location = models.CharField(max_length=120, blank=True)
    device = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self):
        return f"{self.username} {self.result} {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def ok(self):
        return self.result in (self.OK, self.OTP_UNLOCK)


class LoginLockout(models.Model):
    """Consecutive failed sign-ins for one typed username. Keyed by the raw
    string, not a User, so a made-up username behaves exactly like a real one
    (otherwise the lockout itself would reveal which usernames exist)."""

    username = models.CharField(max_length=150, unique=True, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="login_lockouts")
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.username} — {self.failed_attempts} failed attempt(s)"

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())


class AccountSecurity(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="security")
    must_change_password = models.BooleanField(default=False)
    password_changed_at = models.DateTimeField(null=True, blank=True)

    @classmethod
    def for_user(cls, user):
        return cls.objects.get_or_create(user=user)[0]


class PasswordHistory(models.Model):
    """Hashes (never the passwords) of a user's recent passwords. Only the last 5 are kept."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="password_history")
    password = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class AttemptThrottle(models.Model):
    """Counts tries at something (asking for or typing an unlock code) for one
    IP address or one typed identifier, and makes the person wait longer and
    longer once the free tries are used up. See throttle.py."""

    key = models.CharField(max_length=200, unique=True)
    attempts = models.PositiveIntegerField(default=0)
    blocked_until = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(default=timezone.now)


class UnlockCode(models.Model):
    """A one-time 6-digit code emailed to unlock an account. Only a hash of
    the code is stored; it expires quickly and allows few tries."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="unlock_codes")
    code_hash = models.CharField(max_length=256)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class UserSession(models.Model):
    """One row per signed-in browser, so people can see where they're signed in
    and sign out of other devices."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="device_sessions")
    session_key = models.CharField(max_length=40, unique=True)
    device = models.CharField(max_length=120, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-last_seen"]


class VisitorIP(models.Model):
    """A visit counter per IP address. Staff can block an address outright, and
    automatic rules (SecurityRule) can block or flag busy or suspicious ones."""

    ip_address = models.GenericIPAddressField(unique=True)
    hit_count = models.PositiveIntegerField(default=0)
    blocked = models.BooleanField(default=False)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    city = models.CharField(max_length=80, blank=True)
    region = models.CharField(max_length=80, blank=True)
    country = models.CharField(max_length=2, blank=True, help_text="Two-letter country code.")
    located = models.BooleanField(default=False, help_text="Location lookup done (it may still be unknown).")
    recent_hits = models.JSONField(default=list, blank=True, help_text="Times (Unix seconds) of recent visits and sign-in tries, for the rules.")
    rule_alerts = models.JSONField(default=dict, blank=True, help_text="{rule id: Unix time of the last alert}.")
    blocked_at = models.DateTimeField(null=True, blank=True)
    block_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-hit_count"]
        verbose_name = "Visitor IP"

    @staticmethod
    def block(queryset, reason):
        return queryset.filter(blocked=False).update(blocked=True, blocked_at=timezone.now(), block_reason=reason[:200])

    @staticmethod
    def unblock(queryset):
        # Clearing the recent-activity list stops an automatic re-block on the next visit.
        return queryset.filter(blocked=True).update(blocked=False, blocked_at=None, block_reason="", recent_hits=[], rule_alerts={})

    def __str__(self):
        return f"{self.ip_address} ({self.hit_count} visits)"


class SecurityRule(models.Model):
    """One automatic security rule (Security → Rules). Each rule is checked on
    its own whenever an address loads a page or tries to sign in: if its
    conditions match (all, or any one, per `match`), its action happens."""

    ALL, ANY = "all", "any"
    BLOCK_ALERT, BLOCK, ALERT = "block_alert", "block", "alert"
    ACTION_CHOICES = [(BLOCK_ALERT, "Block the address and alert administrators"), (BLOCK, "Block the address quietly"),
                      (ALERT, "Only alert administrators")]
    name = models.CharField(max_length=80)
    enabled = models.BooleanField(default=True)
    match = models.CharField(max_length=3, choices=[(ALL, "All conditions (AND)"), (ANY, "Any condition (OR)")], default=ALL)
    conditions = models.JSONField(default=list, blank=True)
    action = models.CharField(max_length=12, choices=ACTION_CHOICES, default=BLOCK_ALERT)
    times_matched = models.PositiveIntegerField(default=0)
    last_matched_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.name


class PageView(models.Model):
    """One page load by a visitor, for Trending. Recorded on the server (the
    site ships no tracking JavaScript) and skipped for browsers that send Do
    Not Track or Global Privacy Control."""

    visitor_id = models.CharField(max_length=32, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    path = models.CharField(max_length=300, db_index=True)
    referrer = models.CharField(max_length=120, blank=True, help_text="Site they came from, if any.")
    device_type = models.CharField(max_length=10, blank=True, help_text="phone, tablet or computer")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
