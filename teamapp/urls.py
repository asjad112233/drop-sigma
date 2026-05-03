from django.urls import path
from . import views

urlpatterns = [
    path("members/", views.team_members_api),
    path("members/create/", views.create_team_member_api),
    path("members/<int:member_id>/delete/", views.delete_team_member_api),
    path("rules/", views.assignment_rules_api),
    path("rules/create/", views.create_assignment_rule_api),
]
