from django.urls import path
from . import views

urlpatterns = [
    path("api/list/",                  views.api_list,           name="notifications_list"),
    path("api/unread-count/",          views.api_unread_count,   name="notifications_unread_count"),
    path("api/<int:pk>/read/",         views.api_mark_read,      name="notifications_mark_read"),
    path("api/read-all/",              views.api_mark_all_read,  name="notifications_mark_all_read"),
    path("api/<int:pk>/delete/",       views.api_delete,         name="notifications_delete"),
]
