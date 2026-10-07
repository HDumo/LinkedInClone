class SecurityHeadersMiddleware:
    """Adds a Content-Security-Policy and Permissions-Policy to every response.

    The site ships no JavaScript, so scripts are disallowed outright; inline
    <style> is allowed because the base template carries its stylesheet."""

    CSP = (
        "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response
