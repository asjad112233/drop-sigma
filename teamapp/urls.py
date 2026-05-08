from django.urls import path
from . import views

urlpatterns = [
    path("members/", views.team_members_api),
    path("members/create/", views.create_team_member_api),
    path("members/<int:member_id>/delete/", views.delete_team_member_api),
    path("rules/", views.assignment_rules_api),
    path("rules/create/", views.create_assignment_rule_api),
    # Chat
    path("chat/dm/", views.chat_dm_api),
    path("chat/read/", views.chat_mark_read_api),
    path("chat/channels/", views.chat_channels_api),
    path("chat/channels/<int:channel_id>/members/", views.chat_channel_members_api),
    path("chat/channels/<int:channel_id>/members/add/", views.chat_channel_members_add_api),
    path("chat/channels/<int:channel_id>/members/<int:user_id>/remove/", views.chat_channel_members_remove_api),
    path("chat/messages/", views.chat_messages_api),
    path("chat/send/", views.chat_send_api),
    path("chat/upload-image/", views.chat_upload_image_api),
    path("chat/reaction/", views.chat_reaction_api),
    path("chat/messages/<int:msg_id>/delete/", views.chat_delete_message_api),
    path("chat/messages/<int:msg_id>/edit/", views.chat_edit_message_api),
    # Tasks
    path("tasks/",                              views.tasks_list_api),
    path("tasks/create/",                       views.tasks_create_api),
    path("tasks/<int:task_id>/",                views.tasks_detail_api),
    path("tasks/<int:task_id>/comments/",       views.task_comments_api),
]
