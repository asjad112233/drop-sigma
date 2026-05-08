from django.urls import path
from . import views

urlpatterns = [
    path("login/", views.employee_login_page),
    path("logout/", views.employee_logout_view),
    path("dashboard/", views.employee_portal_page),
    path("api/me/", views.employee_me_api),
    path("api/orders/", views.employee_orders_api),
    path("api/emails/", views.employee_emails_api),
    path("api/thread/", views.employee_thread_detail_api),
    path("api/thread/resolve/", views.employee_thread_resolve_api),
    path("api/thread/reopen/", views.employee_thread_reopen_api),
    path("api/tasks/",                    views.employee_tasks_api),
    path("api/tasks/<int:task_id>/",      views.employee_task_update_api),
]
