"""Customer-side public URLs for the RMA app. Mounted at /r/ in core/urls.py."""
from django.urls import path
from . import views

urlpatterns = [
    path("start/<str:order_id>/",         views.cust_start,    name="rma_cust_start"),
    path("start/<str:order_id>/submit/",  views.cust_submit,   name="rma_cust_submit"),
    path("<str:token>/",                  views.cust_track,    name="rma_cust_track"),
    path("<str:token>/poll/",             views.cust_poll,     name="rma_cust_poll"),
    path("<str:token>/message/",          views.cust_message,  name="rma_cust_message"),
    path("<str:token>/tracking/",         views.cust_tracking, name="rma_cust_tracking"),
]
