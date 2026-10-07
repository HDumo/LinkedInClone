"""Approximate location of an IP address from a free offline database (DB-IP
"IP to City Lite", CC BY 4.0, https://db-ip.com), so no visitor IP is sent to
a third party. Install or refresh the file with
`python manage.py update_ip_location_db` (monthly). Without it every lookup
returns None, and country-based security rules simply never match; nothing
breaks. IP locations are approximate: a hint for staff, never a gate."""
import ipaddress
import logging
import threading

from django.conf import settings

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_reader = None
_reader_path = None


def _get_reader():
    global _reader, _reader_path
    path = str(settings.IP_LOCATION_DB)
    with _lock:
        if _reader is not None and _reader_path == path:
            return _reader
        try:
            import maxminddb

            _reader = maxminddb.open_database(path)
            _reader_path = path
        except (FileNotFoundError, ImportError):
            _reader = None
        except Exception:
            logger.exception("Could not open IP location database %s", path)
            _reader = None
        return _reader


def _name(record, key):
    value = (record or {}).get(key)
    return value.get("names", {}).get("en", "") if isinstance(value, dict) else ""


def is_public(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_link_local)


def locate_parts(ip):
    """{"city", "region", "country"} (any may be ""), or None if unknown."""
    if not is_public(ip):
        return None
    reader = _get_reader()
    if reader is None:
        return None
    try:
        record = reader.get(str(ipaddress.ip_address(ip))) or {}
    except Exception:
        logger.exception("IP location lookup failed for %s", ip)
        return None
    subdivisions = record.get("subdivisions") or []
    return {
        "city": _name(record, "city"),
        "region": subdivisions[0].get("names", {}).get("en", "") if subdivisions else "",
        "country": (record.get("country") or {}).get("iso_code", ""),
    }


def locate_label(ip):
    """"Denver, Colorado, US", or "" if unknown."""
    parts = locate_parts(ip) or {}
    return ", ".join(p for p in (parts.get("city"), parts.get("region"), parts.get("country")) if p)[:120]


def reset_cache():
    """For tests and after the database file is replaced."""
    global _reader, _reader_path
    with _lock:
        _reader, _reader_path = None, None
