from django.apps import AppConfig


class TrackingPublicConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tracking_public"
    verbose_name = "Public tracking (track.dropsigma.com)"
