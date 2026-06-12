from django.urls import path
from . import views
from .views import (
    gmail_oauth_start_api,
    gmail_oauth_callback,
    emails_list_api,
    email_detail_api,
    generate_ai_draft_api,
    send_email_reply_api,
    send_email_api,
    sync_inbox_api,
    email_threads_api,
    email_thread_detail_api,
    improve_reply_api,
    ai_playground_api,
    ai_training_profile_api,
    ai_training_snippets_api,
    ai_training_snippet_detail_api,
    ai_training_example_api,
    ai_training_backup_export_api,
    ai_training_backup_preview_api,
    ai_training_backup_apply_api,
    ai_training_backup_snapshots_api,
    ai_training_backup_undo_api,
    category_training_list_api,
    category_training_detail_api,
    auto_suggest_reply_api,
    connect_email_account_api,
    connect_custom_email_api,
    connected_email_api,
    email_settings_api,
    email_settings_update_api,
    disconnect_email_account_api,
    download_attachment_api,
    assign_thread_api,
    assign_thread_multi_api,
    bulk_assign_threads_api,
    unassign_thread_api,
    email_templates_api,
    email_template_detail_api,
    duplicate_template_api,
    reset_template_to_default_api,
    send_test_template_api,
    template_sample_data_api,
    set_category_default_api,
    auto_email_toggle_api,
    gmail_push_webhook,
    gmail_watch_debug_api,
    latest_email_event_api,
    toggle_email_read_api,
    archive_email_thread_api,
    # Labels + Needs Human
    labels_list_api,
    label_detail_api,
    thread_labels_api,
    threads_stats_api,
    needs_human_threads_api,
    thread_ai_status_api,
    empty_folder_api,
    folder_assignments_api,
    move_thread_to_folder_api,
    restore_archived_threads_api,
    permanent_delete_threads_api,
    thread_history_api,
    bulk_mark_threads_read_api,
    team_email_assignments_api,
)

urlpatterns = [
    # =========================
    # 📧 EMAIL SYSTEM
    # =========================

    # Emails List
    path("api/", emails_list_api, name="emails_list_api"),

    # Email Detail
    path("api/<int:email_id>/", email_detail_api, name="email_detail_api"),

    # AI Draft
    path("api/<int:email_id>/generate-ai/", generate_ai_draft_api, name="generate_ai_draft_api"),

    # Send Reply
    path("api/<int:email_id>/send-reply/", send_email_reply_api, name="send_email_reply_api"),

    # Toggle read/unread + archive thread
    path("api/<int:email_id>/toggle-read/", toggle_email_read_api, name="toggle_email_read_api"),
    path("api/<int:email_id>/archive/", archive_email_thread_api, name="archive_email_thread_api"),

    # Send New Email
    path("api/send/", send_email_api, name="send_email_api"),

    # Sync Inbox
    path("api/sync-inbox/", sync_inbox_api, name="sync_inbox_api"),

    # =========================
    # 🧵 THREAD SYSTEM
    # =========================

    path("api/threads/", email_threads_api, name="email_threads_api"),
    path("api/thread/", email_thread_detail_api, name="email_thread_detail_api"),
    path("api/threads/assign/", assign_thread_api, name="assign_thread_api"),
    path("api/threads/assign-multi/", assign_thread_multi_api, name="assign_thread_multi_api"),
    path("api/threads/bulk-assign/", bulk_assign_threads_api, name="bulk_assign_threads_api"),
    path("api/threads/unassign/", unassign_thread_api, name="unassign_thread_api"),

    # =========================
    # 🤖 AI
    # =========================

    path("api/improve-reply/", improve_reply_api, name="improve_reply_api"),
    path("api/ai-playground/", ai_playground_api, name="ai_playground_api"),
    path("api/ai-training/profile/", ai_training_profile_api, name="ai_training_profile_api"),
    path("api/ai-training/snippets/", ai_training_snippets_api, name="ai_training_snippets_api"),
    path("api/ai-training/snippets/<int:snippet_id>/", ai_training_snippet_detail_api, name="ai_training_snippet_detail_api"),
    path("api/ai-training/example/", ai_training_example_api, name="ai_training_example_api"),
    # AI Training backup — export/import + cross-store reuse. The
    # tenant downloads a portable JSON, validates before applying via
    # preview, then commits with mode=replace|merge against the same
    # or a different store under their account.
    path("api/ai-training/backup/export/",    ai_training_backup_export_api,    name="ai_training_backup_export_api"),
    path("api/ai-training/backup/preview/",   ai_training_backup_preview_api,   name="ai_training_backup_preview_api"),
    path("api/ai-training/backup/apply/",     ai_training_backup_apply_api,     name="ai_training_backup_apply_api"),
    # Safety-net: every /apply/ takes an auto-snapshot of the previous
    # state. /snapshots/ lists them, /undo/ replays one. So even an
    # accidental "Replace" mode never permanently nukes the training.
    path("api/ai-training/backup/snapshots/", ai_training_backup_snapshots_api, name="ai_training_backup_snapshots_api"),
    path("api/ai-training/backup/undo/",      ai_training_backup_undo_api,      name="ai_training_backup_undo_api"),
    path("api/ai-training/categories/", category_training_list_api, name="category_training_list_api"),
    path("api/ai-training/categories/<slug:slug>/", category_training_detail_api, name="category_training_detail_api"),

    # AI Training v2 — Q&A + Topics + Test
    path("api/ai-training/v2/qa/",                       views.ai_v2_qa_list_api,        name="ai_v2_qa_list_api"),
    path("api/ai-training/v2/qa/<int:qid>/",             views.ai_v2_qa_answer_api,      name="ai_v2_qa_answer_api"),
    path("api/ai-training/v2/topics/",                   views.ai_v2_topics_api,         name="ai_v2_topics_api"),
    path("api/ai-training/v2/topics/<str:key>/toggle/",  views.ai_v2_topic_toggle_api,   name="ai_v2_topic_toggle_api"),
    path("api/ai-training/v2/snippets/",                 views.ai_v2_snippets_api,       name="ai_v2_snippets_api"),
    path("api/ai-training/v2/test/",                     views.ai_v2_test_api,           name="ai_v2_test_api"),
    path("api/ai-training/v2/overall/",                  views.ai_v2_overall_api,        name="ai_v2_overall_api"),

    path("api/suggest-reply/", auto_suggest_reply_api, name="auto_suggest_reply_api"),

    # =========================
    # 🔥 EMAIL CONNECTION (NEW)
    # =========================

    path("api/connect-email/", connect_email_account_api, name="connect_email_account_api"),
    path("api/connect-custom-email/", connect_custom_email_api, name="connect_custom_email_api"),
    path("api/connected-email/", connected_email_api, name="connected_email_api"),
    path("api/settings/", email_settings_api, name="email_settings_api"),
    path("api/settings/update/", email_settings_update_api, name="email_settings_update_api"),
    path("api/disconnect/", disconnect_email_account_api, name="disconnect_email_account_api"),

    # Attachment download
    path("api/attachment/<int:attachment_id>/", download_attachment_api, name="download_attachment_api"),

    # =========================
    # 📋 EMAIL TEMPLATES
    # =========================

    path("api/templates/", email_templates_api, name="email_templates_api"),
    path("api/templates/<int:template_id>/", email_template_detail_api, name="email_template_detail_api"),
    path("api/templates/<int:template_id>/duplicate/", duplicate_template_api, name="duplicate_template_api"),
    path("api/templates/<int:template_id>/reset/", reset_template_to_default_api, name="reset_template_to_default_api"),
    path("api/templates/<int:template_id>/test/", send_test_template_api, name="send_test_template_api"),
    path("api/templates/<int:template_id>/set-default/", set_category_default_api, name="set_category_default_api"),
    path("api/template-sample-data/", template_sample_data_api, name="template_sample_data_api"),

    # Auto Email Toggle
    path("api/auto-email/", auto_email_toggle_api, name="auto_email_toggle_api"),

    # ─── Custom labels + Needs-Human queue ───────────────────────────────
    path("api/labels/",                     labels_list_api,             name="labels_list_api"),
    path("api/labels/<int:label_id>/",      label_detail_api,            name="label_detail_api"),
    path("api/threads/labels/",             thread_labels_api,           name="thread_labels_api"),
    path("api/threads/stats/",              threads_stats_api,           name="threads_stats_api"),
    path("api/threads/needs-human/",        needs_human_threads_api,     name="needs_human_threads_api"),
    path("api/<int:email_id>/ai-status/",   thread_ai_status_api,        name="thread_ai_status_api"),
    path("api/folders/empty/",              empty_folder_api,            name="empty_folder_api"),
    path("api/folders/assignments/",        folder_assignments_api,      name="folder_assignments_api"),
    path("api/threads/move-folder/",        move_thread_to_folder_api,   name="move_thread_to_folder_api"),
    # Archive-folder bulk actions
    path("api/threads/restore/",            restore_archived_threads_api, name="restore_archived_threads_api"),
    path("api/threads/permanent-delete/",   permanent_delete_threads_api, name="permanent_delete_threads_api"),
    path("api/threads/history/",            thread_history_api,           name="thread_history_api"),
    path("api/threads/bulk-mark-read/",     bulk_mark_threads_read_api,   name="bulk_mark_threads_read_api"),
    # Admin → assigns email folders / labels / capabilities to a team member
    path("api/team/<int:member_id>/email-assignments/", team_email_assignments_api, name="team_email_assignments_api"),

    # Gmail OAuth2
    path("oauth/start/", gmail_oauth_start_api, name="gmail_oauth_start"),
    path("oauth/callback/", gmail_oauth_callback, name="gmail_oauth_callback"),

    # Real-time Gmail push (Cloud Pub/Sub)
    path("webhook/gmail-push/", gmail_push_webhook, name="gmail_push_webhook"),
    path("api/gmail-watch-debug/", gmail_watch_debug_api, name="gmail_watch_debug_api"),
    path("api/latest-event/", latest_email_event_api, name="latest_email_event_api"),
]