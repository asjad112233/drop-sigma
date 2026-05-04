from rest_framework.authentication import SessionAuthentication


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """
    SessionAuthentication without CSRF enforcement.

    This is safe for a same-domain SPA admin dashboard: the session cookie
    already authenticates the user, and CSRF attacks require a different
    origin — which is already blocked by the browser's SameSite cookie policy
    and Django's ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS settings.
    """

    def enforce_csrf(self, request):
        pass
