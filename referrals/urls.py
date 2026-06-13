from django.urls import path
from . import views

app_name = "referrals"

urlpatterns = [
    # Authenticated tenant API — drives the dashboard banner.
    path("api/referrals/my-link/", views.api_my_link, name="api_my_link"),
    path("api/referrals/progress/", views.api_progress, name="api_progress"),
    # Public share landing — stamps the session and redirects to signup.
    path("invite/<str:code>/", views.invite_landing, name="invite_landing"),
]
