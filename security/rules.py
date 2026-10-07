"""Automatic security rules (Security → Rules), the IP whitelist and blocking.

Each SecurityRule is checked on its own every time an address loads a page or
tries to sign in (VisitorLoggingMiddleware). A rule matches when all of its
conditions are true (match "all", AND) or any one is (match "any", OR); then
its action happens: block the address (and alert administrators), or only
alert. Rules don't depend on each other, so "A and B" plus "A and C" as two
rules gives "A and (B or C)".

Whitelisted addresses are never blocked, by a rule or by hand. Private and
loopback addresses (the server itself, your office network behind a proxy)
are never blocked either. An address whose country is unknown never matches
a country condition, so a gap in the location database can't block anyone."""
import ipaddress
import logging
import time

from django.utils import timezone

logger = logging.getLogger(__name__)

CONDITIONS = {
    # type: (label shown in the portal, [parameter names])
    "outside_countries": ("Visitor is outside these countries", ["countries"]),
    "in_countries": ("Visitor is in one of these countries", ["countries"]),
    "unknown_country": ("Visitor's country is unknown", []),
    "activity": ("Page visits or sign-in tries in a short time", ["count", "minutes"]),
    "failed_logins": ("Failed sign-ins from the address in a short time", ["count", "minutes"]),
    "total_visits": ("Total page visits ever", ["count"]),
    "path": ("Opened a page whose address contains", ["text"]),
    "user_agent": ("Browser or program name contains", ["text"]),
}
MAX_MINUTES = 1440
MAX_HITS_KEPT = 2000


def is_exempt_address(ip):
    """Addresses that can never be blocked: the server itself and private networks."""
    try:
        addr = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return True  # no usable address: nothing to block
    return addr.is_private or addr.is_loopback or addr.is_link_local


# --- Whitelist -----------------------------------------------------------

def parse_whitelist(text):
    """([(network, note)], [lines that aren't addresses])."""
    networks, bad = [], []
    for line in (text or "").splitlines():
        entry, _, note = line.partition("#")
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append((ipaddress.ip_network(entry, strict=False), note.strip()))
        except ValueError:
            bad.append(entry)
    return networks, bad


def is_whitelisted(ip, whitelist_text=None):
    if not ip:
        return False
    if whitelist_text is None:
        from .models import SecurityConfig

        whitelist_text = SecurityConfig.get().whitelist
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in net for net, _ in parse_whitelist(whitelist_text)[0] if net.version == address.version)


def remove_from_whitelist(ip):
    """Take ip off the whitelist so it can be blocked. Lines for exactly this
    address are removed; returns whitelisted ranges that still cover it."""
    from .models import SecurityConfig

    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return []
    cfg = SecurityConfig.get()
    keep, ranges, changed = [], [], False
    for line in (cfg.whitelist or "").splitlines():
        entry = line.partition("#")[0].strip()
        try:
            net = ipaddress.ip_network(entry, strict=False) if entry else None
        except ValueError:
            net = None
        if net is not None and net.version == address.version and address in net:
            if net.num_addresses == 1:
                changed = True
                continue
            ranges.append(str(net))
        keep.append(line)
    if changed:
        cfg.whitelist = "\n".join(keep).strip()
        cfg.save(update_fields=["whitelist", "updated_at"])
    return ranges


def add_to_whitelist(ips, note=""):
    """Put addresses on the whitelist, unblocking them first."""
    from .models import SecurityConfig, VisitorIP

    VisitorIP.unblock(VisitorIP.objects.filter(ip_address__in=ips))
    cfg = SecurityConfig.get()
    new = [ip + (f"  # {note}" if note else "") for ip in ips if not is_whitelisted(ip, cfg.whitelist)]
    if new:
        cfg.whitelist = "\n".join(filter(None, [(cfg.whitelist or "").strip()] + new))
        cfg.save(update_fields=["whitelist", "updated_at"])
    return len(new)


def unblock_whitelisted():
    """After the whitelist changes: let every address on it back in."""
    from .models import SecurityConfig, VisitorIP

    text = SecurityConfig.get().whitelist
    ids = [v.pk for v in VisitorIP.objects.filter(blocked=True) if is_whitelisted(v.ip_address, text)]
    return VisitorIP.unblock(VisitorIP.objects.filter(pk__in=ids))


def block_ips(queryset, reason):
    """Block visitors, taking them off the whitelist first. Returns (blocked
    count, [(ip, range)] left unblocked because a whitelisted range covers them)."""
    from .models import VisitorIP

    stuck, ok = [], []
    for visitor in queryset:
        ranges = remove_from_whitelist(visitor.ip_address)
        if ranges:
            stuck.append((visitor.ip_address, ranges[0]))
        else:
            ok.append(visitor.pk)
    return VisitorIP.block(VisitorIP.objects.filter(pk__in=ok), reason), stuck


# --- Checking a rule's settings ----------------------------------------------

def clean_rule(data):
    """(fields, error). fields are ready to set on a SecurityRule."""
    from .models import SecurityRule

    name = " ".join(str(data.get("name") or "").split())[:80]
    if not name:
        return None, "Give the rule a name."
    match = data.get("match") or SecurityRule.ALL
    action = data.get("action") or SecurityRule.BLOCK_ALERT
    if match not in (SecurityRule.ALL, SecurityRule.ANY) or action not in dict(SecurityRule.ACTION_CHOICES):
        return None, "Pick how the conditions combine and what the rule does."
    raw = data.get("conditions")
    if not isinstance(raw, list) or not raw:
        return None, "Add at least one condition."
    if len(raw) > 10:
        return None, "A rule can have up to 10 conditions."
    conditions = []
    for c in raw:
        c = c if isinstance(c, dict) else {}
        kind = c.get("type")
        if kind not in CONDITIONS:
            return None, "Pick a type for every condition."
        label = CONDITIONS[kind][0]
        clean = {"type": kind}
        if "countries" in CONDITIONS[kind][1]:
            codes = [x.strip().upper() for x in str(c.get("countries") or "").replace(";", ",").split(",") if x.strip()]
            if not codes or any(len(x) != 2 or not x.isalpha() for x in codes):
                return None, f"“{label}”: enter two-letter country codes separated by commas, like US, CA."
            clean["countries"] = sorted(set(codes))
        if "count" in CONDITIONS[kind][1]:
            try:
                count = int(c.get("count"))
            except (TypeError, ValueError):
                count = 0
            if not 1 <= count <= 1000000:
                return None, f"“{label}”: enter how many (1 or more)."
            clean["count"] = count
        if "minutes" in CONDITIONS[kind][1]:
            try:
                minutes = int(c.get("minutes"))
            except (TypeError, ValueError):
                minutes = 0
            if not 1 <= minutes <= MAX_MINUTES:
                return None, f"“{label}”: enter minutes from 1 to {MAX_MINUTES} (one day)."
            clean["minutes"] = minutes
        if "text" in CONDITIONS[kind][1]:
            text = str(c.get("text") or "").strip()[:100]
            if len(text) < 2:
                return None, f"“{label}”: enter at least 2 characters to look for."
            clean["text"] = text
        conditions.append(clean)
    return {"name": name, "match": match, "action": action, "conditions": conditions,
            "enabled": bool(data.get("enabled", True))}, None


def describe(rule):
    """The rule in one plain sentence, for the portal and block reasons."""
    parts = []
    for c in rule.conditions:
        kind = c.get("type")
        if kind == "outside_countries":
            parts.append("outside " + ", ".join(c["countries"]))
        elif kind == "in_countries":
            parts.append("in " + ", ".join(c["countries"]))
        elif kind == "unknown_country":
            parts.append("country unknown")
        elif kind == "activity":
            parts.append(f"{c['count']}+ visits or sign-in tries within {c['minutes']} min")
        elif kind == "failed_logins":
            parts.append(f"{c['count']}+ failed sign-ins within {c['minutes']} min")
        elif kind == "total_visits":
            parts.append(f"{c['count']}+ visits in total")
        elif kind == "path":
            parts.append(f"opened a page containing “{c['text']}”")
        elif kind == "user_agent":
            parts.append(f"browser contains “{c['text']}”")
    return (" and " if rule.match == "all" else " or ").join(parts)


# --- Running the rules -----------------------------------------------------------

def _hits_within(hits, minutes, now):
    since = now - minutes * 60
    return sum(1 for t in hits if t >= since)


def _failed_logins(ip, minutes):
    from datetime import timedelta

    from .models import LoginEvent

    return (LoginEvent.objects.filter(ip_address=ip, created_at__gte=timezone.now() - timedelta(minutes=minutes))
            .exclude(result__in=[LoginEvent.OK, LoginEvent.OTP_UNLOCK]).count())


def condition_true(c, visitor, path="", user_agent="", now=None):
    now = now or time.time()
    kind = c.get("type")
    country = (visitor.country or "").upper()
    if kind == "outside_countries":
        return bool(country) and country not in c["countries"]
    if kind == "in_countries":
        return bool(country) and country in c["countries"]
    if kind == "unknown_country":
        return visitor.located and not country
    if kind == "activity":
        return _hits_within(visitor.recent_hits or [], c["minutes"], now) >= c["count"]
    if kind == "failed_logins":
        return _failed_logins(visitor.ip_address, c["minutes"]) >= c["count"]
    if kind == "total_visits":
        return visitor.hit_count >= c["count"]
    if kind == "path":
        return c["text"].lower() in (path or "").lower()
    if kind == "user_agent":
        return c["text"].lower() in (user_agent or "").lower()
    return False


def rule_matches(rule, visitor, path="", user_agent="", now=None):
    results = (condition_true(c, visitor, path, user_agent, now) for c in rule.conditions)
    return all(results) if rule.match == "all" else any(results)


def _alert_window(rule):
    minutes = [c["minutes"] for c in rule.conditions if "minutes" in c]
    return max(minutes or [60]) * 60


def record_and_check(visitor, path="", user_agent=""):
    """Remember this visit or sign-in try, then run every enabled rule.
    Returns True if a rule blocked the address just now. Never raises."""
    from .models import SecurityRule, VisitorIP

    try:
        rules = list(SecurityRule.objects.filter(enabled=True))
        now = time.time()
        keep = max([c["minutes"] for r in rules for c in r.conditions if c.get("type") == "activity"] or [60])
        hits = [t for t in (visitor.recent_hits or []) if t >= now - keep * 60][-MAX_HITS_KEPT + 1:] + [int(now)]
        visitor.recent_hits = hits
        VisitorIP.objects.filter(pk=visitor.pk).update(recent_hits=hits)
        if not rules or visitor.blocked or is_exempt_address(visitor.ip_address) or is_whitelisted(visitor.ip_address):
            return False
        for rule in rules:
            if rule_matches(rule, visitor, path, user_agent, now):
                _apply(rule, visitor, now)
                if visitor.blocked:
                    return True
    except Exception:
        logger.exception("Security rules could not be checked for %s", visitor.ip_address)
    return False


def _apply(rule, visitor, now):
    from django.db.models import F
    from django.urls import reverse

    from notifications.models import notify_admins

    from .models import SecurityRule, VisitorIP

    where = ", ".join(p for p in (visitor.city, visitor.region, visitor.country) if p) or "an unknown place"
    details = describe(rule)
    link = reverse("security_visitors") + f"?q={visitor.ip_address}"
    if rule.action in (SecurityRule.BLOCK, SecurityRule.BLOCK_ALERT):
        reason = f"Rule “{rule.name}”: {details}"[:200]
        blocked = VisitorIP.objects.filter(pk=visitor.pk, blocked=False).update(
            blocked=True, blocked_at=timezone.now(), block_reason=reason)
        visitor.blocked = True
        if not blocked:
            return
        SecurityRule.objects.filter(pk=rule.pk).update(times_matched=F("times_matched") + 1, last_matched_at=timezone.now())
        if rule.action == SecurityRule.BLOCK_ALERT:
            notify_admins(f"Security rule “{rule.name}” blocked {visitor.ip_address} ({where}): {details}", link)
        return
    last = (visitor.rule_alerts or {}).get(str(rule.pk), 0)
    if now - last < _alert_window(rule):
        return  # already told administrators about this address recently
    alerts = {**(visitor.rule_alerts or {}), str(rule.pk): int(now)}
    VisitorIP.objects.filter(pk=visitor.pk).update(rule_alerts=alerts)
    visitor.rule_alerts = alerts
    SecurityRule.objects.filter(pk=rule.pk).update(times_matched=F("times_matched") + 1, last_matched_at=timezone.now())
    notify_admins(f"Security rule “{rule.name}” flagged {visitor.ip_address} ({where}): {details}", link)


def preview(rule, limit=10):
    """Addresses seen in the last day that this rule would match now (page and
    browser conditions can't be checked afterwards)."""
    from datetime import timedelta

    from .models import VisitorIP

    rows = VisitorIP.objects.filter(last_seen__gte=timezone.now() - timedelta(days=1)).order_by("-last_seen")[:2000]
    now = time.time()
    matched = [v for v in rows if not is_whitelisted(v.ip_address) and not is_exempt_address(v.ip_address)
               and rule_matches(rule, v, now=now)]
    return len(matched), [{"ip": v.ip_address, "country": v.country or "?", "visits": v.hit_count, "blocked": v.blocked}
                          for v in matched[:limit]]
