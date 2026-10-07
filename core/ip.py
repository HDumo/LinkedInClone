import ipaddress


def client_ip(request):
    """The real client IP behind Nginx's reverse proxy.

    deploy/nginx.conf sets `X-Forwarded-For: $proxy_add_x_forwarded_for`,
    which APPENDS Nginx's view of the connecting IP to whatever the client
    already sent. Only the LAST hop is trustworthy, so take the last entry;
    taking the first would let anyone spoof their address with one header.
    Falls back to the socket address if the header is missing or garbage
    (gunicorn reached directly, local development)."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        candidate = xff.split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            pass
    return request.META.get("REMOTE_ADDR")
