from django.contrib.auth.models import User


class ImpersonationMiddleware:
    """
    If a superuser has set session['impersonate_id'], swap request.user with
    the impersonated user for non-superadmin paths (so the tenant dashboard
    shows that tenant's data).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        impersonate_id = request.session.get("impersonate_id")
        if (
            impersonate_id
            and request.user.is_authenticated
            and request.user.is_superuser
            and not request.path.startswith("/superadmin/")
            and not request.path.startswith("/admin/")
        ):
            try:
                impersonated = User.objects.get(pk=impersonate_id)
                request.user = impersonated
                request._impersonating = True
            except User.DoesNotExist:
                request.session.pop("impersonate_id",    None)
                request.session.pop("impersonate_name",  None)
                request.session.pop("impersonate_email", None)

        return self.get_response(request)
