import os
import requests

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from django.db.models import Q

from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from email.utils import parseaddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

import imaplib
import smtplib
import socket
import ssl

from stores.models import Store


def _get_gmail_access_token(refresh_token):
    from django.conf import settings as _settings
    client_id = getattr(_settings, "GOOGLE_CLIENT_ID", "") or os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = getattr(_settings, "GOOGLE_CLIENT_SECRET", "") or os.getenv("GOOGLE_CLIENT_SECRET", "")
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    }, timeout=15)
    if not resp.ok:
        raise Exception(f"Token refresh failed: {resp.text[:200]}")
    return resp.json()["access_token"]


def _gmail_api_send(account, recipient, subject, html_body, files=None,
                    in_reply_to=None, references=None, thread_id=None):
    """
    Send via Gmail HTTP API.
    Pass in_reply_to + references (RFC-822 Message-IDs) and thread_id (Gmail's threadId)
    to keep the reply in the same Gmail conversation thread.
    """
    import base64 as _b64
    from email.mime.multipart import MIMEMultipart as _MMP
    from email.mime.text import MIMEText as _MMT
    from email.mime.base import MIMEBase as _MMB
    from email import encoders as _enc

    access_token = _get_gmail_access_token(account.oauth_refresh_token)

    msg = _MMP()
    msg["From"] = account.email
    msg["To"] = recipient
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(_MMT(html_body, "html"))

    for f in (files or []):
        part = _MMB("application", "octet-stream")
        part.set_payload(f.read())
        _enc.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{f.name}"')
        msg.attach(part)

    raw = _b64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    resp = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if not resp.ok:
        raise Exception(f"Gmail API error: {resp.text[:200]}")


def _brevo_send(from_email, to_email, subject, html_body, attachments=None):
    """Send via Brevo HTTP API — works on Railway (no SMTP ports needed)."""
    import requests as _req
    import base64 as _b64
    import os as _os
    api_key = _os.getenv("BREVO_API_KEY", "")
    if not api_key:
        raise Exception("BREVO_API_KEY not set in environment variables.")
    payload = {
        "sender": {"email": from_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
    }
    if attachments:
        payload["attachment"] = [
            {"name": f.name, "content": _b64.b64encode(f.read()).decode()}
            for f in attachments
        ]
    resp = _req.post(
        "https://api.brevo.com/v3/smtp/email",
        json=payload,
        headers={"api-key": api_key, "content-type": "application/json"},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        raise Exception(f"Brevo error {resp.status_code}: {resp.text[:200]}")
from .models import EmailMessage, EmailAccount, EmailAttachment, EmailThreadAssignment, EmailTemplate
from .serializers import EmailMessageSerializer
from .services import sync_gmail_inbox, generate_ai_reply, generate_ai_text, generate_smart_suggestion, render_template_content, SAMPLE_TEMPLATE_DATA, build_template_context


def extract_clean_email(value):
    if not value:
        return ""

    name, addr = parseaddr(value)
    return (addr or value).strip().lower()


def _smtp_send_direct(account, recipient, subject, html_body, files=None,
                      in_reply_to=None, references=None, thread_id=None):
    """Send via stored SMTP credentials (custom hosting / non-Gmail accounts).
    in_reply_to / references RFC-822 Message-IDs keep replies threaded."""
    msg = MIMEMultipart("alternative")
    msg["From"] = account.email
    msg["To"] = recipient
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(MIMEText(html_body, "html"))

    for f in (files or []):
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{f.name}"')
        msg.attach(part)

    port = account.smtp_port
    host = account.smtp_host
    password = account.app_password

    if port == 465:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as server:
            server.login(account.email, password)
            server.sendmail(account.email, recipient, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.login(account.email, password)
            server.sendmail(account.email, recipient, msg.as_string())


def send_email_with_store_account(store, recipient, subject, body, files=None,
                                  in_reply_to=None, references=None, thread_id=None):
    """
    Send an email from the store's connected inbox.
    in_reply_to + references = RFC Message-IDs (e.g. '<ABC@mail.gmail.com>') so
    the reply threads under the original conversation in the recipient's inbox.
    thread_id = Gmail's internal thread id (only meaningful for OAuth/Gmail API path).
    """
    account = EmailAccount.objects.filter(store=store, is_active=True).first()

    if not account:
        raise Exception("No connected email account found for this store.")

    if account.auth_type == "oauth" and account.oauth_refresh_token:
        _gmail_api_send(account, recipient, subject, body, files=files,
                        in_reply_to=in_reply_to, references=references, thread_id=thread_id)
    elif account.auth_type == "password" and account.app_password:
        _smtp_send_direct(account, recipient, subject, body, files=files,
                          in_reply_to=in_reply_to, references=references, thread_id=thread_id)
    else:
        _brevo_send(account.email, recipient, subject, body, attachments=files)

    return account.email


def get_thread_contact(email_obj):
    sender = extract_clean_email(email_obj.sender)
    recipient = extract_clean_email(email_obj.recipient)

    account = EmailAccount.objects.filter(store=email_obj.store, is_active=True).first()
    store_email = extract_clean_email(account.email) if account else extract_clean_email(settings.DEFAULT_FROM_EMAIL)

    if sender == store_email:
        return recipient

    return sender


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def connect_email_account_api(request):
    store_id = request.data.get("store_id")
    email = request.data.get("email")
    app_password = request.data.get("app_password")

    if not store_id or not email or not app_password:
        return Response({
            "success": False,
            "message": "Store, email and app password are required."
        }, status=400)

    store = Store.objects.filter(id=store_id).first()

    if not store:
        return Response({
            "success": False,
            "message": "Store not found."
        }, status=404)

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
        imap.login(email, app_password)
        imap.logout()
    except Exception as e:
        return Response({
            "success": False,
            "message": f"IMAP login failed: {str(e)}"
        }, status=400)

    account, created = EmailAccount.objects.update_or_create(
        store=store,
        email=email,
        defaults={
            "app_password": app_password,
            "imap_host": "imap.gmail.com",
            "imap_port": 993,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "is_active": True,
        }
    )

    return Response({
        "success": True,
        "message": "Email connected successfully.",
        "email": account.email,
        "store_id": store.id
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def connect_custom_email_api(request):
    """Connect a custom hosting email via SMTP + IMAP credentials."""
    store_id  = request.data.get("store_id")
    email     = request.data.get("email", "").strip()
    password  = request.data.get("password", "").strip()
    imap_host = request.data.get("imap_host", "").strip()
    imap_port = int(request.data.get("imap_port", 993))
    smtp_host = request.data.get("smtp_host", "").strip()
    smtp_port = int(request.data.get("smtp_port", 587))

    if not all([store_id, email, password, imap_host, smtp_host]):
        return Response({"success": False, "message": "All fields are required."}, status=400)

    store = Store.objects.filter(id=store_id).first()
    if not store:
        return Response({"success": False, "message": "Store not found."}, status=404)

    # Test IMAP
    try:
        imap = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=15)
        imap.login(email, password)
        imap.logout()
    except Exception as e:
        return Response({"success": False, "message": f"IMAP connection failed: {str(e)}"}, status=400)

    # Test SMTP
    try:
        ctx = ssl.create_default_context()
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=15) as s:
                s.login(email, password)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(email, password)
    except Exception as e:
        return Response({"success": False, "message": f"SMTP connection failed: {str(e)}"}, status=400)

    account, _ = EmailAccount.objects.update_or_create(
        store=store,
        email=email,
        defaults={
            "app_password": password,
            "imap_host":    imap_host,
            "imap_port":    imap_port,
            "smtp_host":    smtp_host,
            "smtp_port":    smtp_port,
            "auth_type":    "password",
            "is_active":    True,
        },
    )

    return Response({
        "success":  True,
        "message":  "Custom email connected successfully.",
        "email":    account.email,
        "store_id": store.id,
    })


@api_view(["GET"])
def gmail_oauth_start_api(request):
    import urllib.parse
    from django.conf import settings as _settings
    store_id = request.GET.get("store_id")
    client_id = getattr(_settings, "GOOGLE_CLIENT_ID", "") or os.getenv("GOOGLE_CLIENT_ID", "")
    redirect_uri = getattr(_settings, "GOOGLE_OAUTH_REDIRECT_URI", "") or os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/emails/oauth/callback/")
    if not client_id:
        return Response({"success": False, "message": "Google OAuth not configured."}, status=500)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://mail.google.com/ email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": store_id,
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return Response({"success": True, "url": url})


def gmail_oauth_callback(request):
    import logging
    from django.shortcuts import redirect as _redirect
    from django.conf import settings as _settings

    logger = logging.getLogger(__name__)

    code = request.GET.get("code")
    store_id = request.GET.get("state")

    if request.GET.get("error") or not code:
        return _redirect("/dashboard/?gmail_error=denied")

    client_id = getattr(_settings, "GOOGLE_CLIENT_ID", "") or os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = getattr(_settings, "GOOGLE_CLIENT_SECRET", "") or os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = (
        getattr(_settings, "GOOGLE_OAUTH_REDIRECT_URI", "")
        or os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/emails/oauth/callback/")
    )

    if not client_id or not client_secret:
        return _redirect("/dashboard/?gmail_error=not_configured")

    # Exchange authorization code for tokens
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
            timeout=15,
        )
    except Exception as exc:
        logger.error("Gmail OAuth token exchange failed: %s", exc)
        return _redirect("/dashboard/?gmail_error=network_error")

    if not token_resp.ok:
        logger.error("Gmail OAuth token exchange HTTP %s: %s", token_resp.status_code, token_resp.text[:300])
        return _redirect("/dashboard/?gmail_error=auth_failed")

    try:
        tokens = token_resp.json()
    except Exception:
        return _redirect("/dashboard/?gmail_error=bad_response")

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not refresh_token:
        return _redirect("/dashboard/?gmail_error=no_refresh_token")

    # Fetch Gmail address
    try:
        user_resp = requests.get(
            "https://www.googleapis.com/oauth2/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        email = user_resp.json().get("email", "")
    except Exception as exc:
        logger.error("Gmail OAuth userinfo fetch failed: %s", exc)
        return _redirect("/dashboard/?gmail_error=userinfo_failed")

    if not email:
        return _redirect("/dashboard/?gmail_error=no_email")

    store = Store.objects.filter(id=store_id).first()
    if not store:
        return _redirect("/dashboard/?gmail_error=store_not_found")

    try:
        EmailAccount.objects.update_or_create(
            store=store,
            email=email,
            defaults={
                "app_password": "",
                "oauth_refresh_token": refresh_token,
                "auth_type": "oauth",
                "imap_host": "imap.gmail.com",
                "imap_port": 993,
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "is_active": True,
            },
        )
    except Exception as exc:
        logger.error("Gmail OAuth save account failed: %s", exc)
        return _redirect("/dashboard/?gmail_error=save_failed")

    return _redirect("/dashboard/?gmail_connected=1")


@api_view(["GET"])
def connected_email_api(request):
    store_id = request.GET.get("store_id")

    accounts = EmailAccount.objects.filter(store_id=store_id, is_active=True)

    if not accounts.exists():
        return Response({
            "success": True,
            "connected": False,
            "email": None,
            "accounts": []
        })

    accounts_list = [{"id": a.id, "email": a.email, "auth_type": a.auth_type} for a in accounts]
    return Response({
        "success": True,
        "connected": True,
        "email": accounts_list[0]["email"],
        "accounts": accounts_list
    })


_SETTINGS_FIELDS = [
    "fetch_limit", "sync_folder", "mark_read_in_gmail", "sync_on_tab_focus",
    "live_sync_enabled", "sync_interval",
    "ai_tone", "ai_language", "ai_auto_suggest", "ai_auto_draft", "ai_include_order",
    "ai_reply_mode", "ai_custom_instructions", "ai_use_signature",
    "signature",
    "notify_browser", "notify_sound", "notify_unread_only", "notify_assigned_only",
    "auto_close_after_reply", "auto_mark_read_on_open", "show_cc_bcc",
]


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def email_settings_api(request):
    store_id = request.GET.get("store_id")
    account_id = request.GET.get("account_id")

    if account_id:
        account = EmailAccount.objects.filter(id=account_id, store_id=store_id).first()
    else:
        account = EmailAccount.objects.filter(store_id=store_id).first()

    if not account:
        return Response({"success": True, "connected": False})

    s = {f: getattr(account, f) for f in _SETTINGS_FIELDS}
    s["email"] = account.email
    s["account_id"] = account.id
    s["imap_host"] = account.imap_host
    s["is_active"] = account.is_active
    s["last_synced"] = account.last_synced.isoformat() if account.last_synced else None

    return Response({"success": True, "connected": True, "settings": s})


@csrf_exempt
@api_view(["PATCH"])
@authentication_classes([])
@permission_classes([AllowAny])
def email_settings_update_api(request):
    store_id = request.data.get("store_id")
    account_id = request.data.get("account_id")
    new_settings = request.data.get("settings", {})

    if account_id:
        account = EmailAccount.objects.filter(id=account_id, store_id=store_id).first()
    else:
        account = EmailAccount.objects.filter(store_id=store_id).first()

    if not account:
        return Response({"success": False, "message": "No email account found."}, status=404)

    for key, value in new_settings.items():
        if key in _SETTINGS_FIELDS:
            setattr(account, key, value)

    account.save()
    return Response({"success": True})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def disconnect_email_account_api(request):
    store_id = request.data.get("store_id")
    account_id = request.data.get("account_id")

    if not store_id:
        return Response({"success": False, "message": "store_id required."}, status=400)

    if account_id:
        account = EmailAccount.objects.filter(id=account_id, store_id=store_id).first()
    else:
        account = EmailAccount.objects.filter(store_id=store_id).first()

    if not account:
        return Response({"success": False, "message": "No email account found."}, status=404)

    account.delete()

    # If no more accounts for this store, clean up messages/threads
    if not EmailAccount.objects.filter(store_id=store_id).exists():
        EmailMessage.objects.filter(store_id=store_id).delete()
        EmailThreadAssignment.objects.filter(store_id=store_id).delete()

    return Response({"success": True, "message": "Email account disconnected."})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def send_email_api(request):
    subject = request.data.get("subject")
    body = request.data.get("body")
    recipient = request.data.get("recipient")
    store_id = request.data.get("store_id")

    if not subject or not body or not recipient:
        return Response({
            "success": False,
            "message": "Subject, body and recipient required."
        }, status=400)

    store = Store.objects.filter(id=store_id).first()

    if not store:
        return Response({
            "success": False,
            "message": "Store not found."
        }, status=404)

    files = request.FILES.getlist("attachments")

    try:
        from_email = send_email_with_store_account(
            store=store,
            recipient=recipient,
            subject=subject,
            body=body,
            files=files,
        )

        print("🔥 USING STORE SMTP COMPOSE 🔥")
        print("FROM EMAIL:", from_email)

    except Exception as e:
        return Response({
            "success": False,
            "message": str(e)
        }, status=400)

    email_obj = EmailMessage.objects.create(
        store=store,
        sender=from_email,
        recipient=recipient,
        subject=subject,
        body=body,
        status="replied",
        is_read=True,
        raw_data={
            "type": "outgoing",
            "source": "compose",
            "sent_from": from_email
        }
    )

    for f in files:
        f.seek(0)
        EmailAttachment.objects.create(
            email=email_obj,
            filename=f.name,
            content_type=f.content_type,
            file=f,
            size=f.size,
        )

    return Response({
        "success": True,
        "message": "Email sent and saved successfully.",
        "email_id": email_obj.id,
        "sent_from": from_email
    })


@api_view(["GET"])
def emails_list_api(request):
    store_id = request.GET.get("store_id")
    status = request.GET.get("status")

    # Guard: if a store_id is provided but no active EmailAccount is connected,
    # return empty (orphan messages from a prior disconnected inbox must not show).
    if store_id and not EmailAccount.objects.filter(store_id=store_id, is_active=True).exists():
        return Response({"success": True, "count": 0, "emails": []})

    emails = EmailMessage.objects.all().order_by("-created_at")

    if store_id:
        emails = emails.filter(store_id=store_id)

    if status:
        emails = emails.filter(status=status)

    serializer = EmailMessageSerializer(emails, many=True)

    return Response({
        "success": True,
        "count": emails.count(),
        "emails": serializer.data
    })


@api_view(["GET"])
def email_detail_api(request, email_id):
    email = get_object_or_404(EmailMessage, id=email_id)
    serializer = EmailMessageSerializer(email)

    return Response({
        "success": True,
        "email": serializer.data
    })


@api_view(["GET"])
def email_threads_api(request):
    store_id = request.GET.get("store_id")

    # Guard: empty result if no active EmailAccount for this store.
    if store_id and not EmailAccount.objects.filter(store_id=store_id, is_active=True).exists():
        return Response({"success": True, "count": 0, "threads": []})

    emails = EmailMessage.objects.all().order_by("-created_at")

    if store_id:
        emails = emails.filter(store_id=store_id)

    assignments = {}
    resolved_map = {}
    if store_id:
        for ta in EmailThreadAssignment.objects.filter(store_id=store_id).select_related("assigned_to").prefetch_related("co_assignees"):
            key = ta.contact.lower()
            resolved_map[key] = {
                "is_resolved": ta.is_resolved,
                "resolved_at": ta.resolved_at.isoformat() if ta.resolved_at else None,
            }
            co_list = [{"id": m.id, "name": m.name, "email": m.email, "role": m.role} for m in ta.co_assignees.all()]
            if ta.assigned_to:
                assignments[key] = {
                    "id": ta.assigned_to.id,
                    "name": ta.assigned_to.name,
                    "email": ta.assigned_to.email,
                    "role": ta.assigned_to.role,
                    "co_assignees": co_list,
                }
            elif co_list:
                assignments[key] = {**co_list[0], "co_assignees": co_list}

    threads = {}

    for email_obj in emails:
        contact = get_thread_contact(email_obj)

        if not contact:
            continue

        if contact not in threads:
            threads[contact] = {
                "contact": contact,
                "name": getattr(email_obj, "sender_name", None) or contact,
                "latest_subject": email_obj.subject or "No subject",
                "latest_body": email_obj.body or "",
                "latest_status": email_obj.status,
                "latest_time": email_obj.created_at,
                "total_messages": 0,
                "new_count": 0,
                "drafted_count": 0,
                "replied_count": 0,
                "unread_count": 0,
                "is_read": True,
            }

        threads[contact]["total_messages"] += 1

        if email_obj.status == "new":
            threads[contact]["new_count"] += 1

        if email_obj.status == "drafted":
            threads[contact]["drafted_count"] += 1

        if email_obj.status == "replied":
            threads[contact]["replied_count"] += 1

        if not getattr(email_obj, "is_read", True):
            threads[contact]["unread_count"] += 1
            threads[contact]["is_read"] = False

        if email_obj.created_at > threads[contact]["latest_time"]:
            threads[contact]["name"] = getattr(email_obj, "sender_name", None) or contact
            threads[contact]["latest_subject"] = email_obj.subject or "No subject"
            threads[contact]["latest_body"] = email_obj.body or ""
            threads[contact]["latest_status"] = email_obj.status
            threads[contact]["latest_time"] = email_obj.created_at

    results = list(threads.values())
    results.sort(key=lambda x: x["latest_time"], reverse=True)

    for item in results:
        item["latest_time"] = item["latest_time"].isoformat()
        key = (item["contact"] or "").lower()
        item["assigned_to"] = assignments.get(key)
        res = resolved_map.get(key, {})
        item["is_resolved"] = res.get("is_resolved", False)
        item["resolved_at"] = res.get("resolved_at")

    return Response({
        "success": True,
        "count": len(results),
        "threads": results
    })


@api_view(["GET"])
def email_thread_detail_api(request):
    store_id = request.GET.get("store_id")
    contact = extract_clean_email(request.GET.get("contact"))

    if not contact:
        return Response({
            "success": False,
            "message": "Contact email is required."
        }, status=400)

    emails = EmailMessage.objects.all().order_by("created_at")

    if store_id:
        emails = emails.filter(store_id=store_id)

    thread_emails = []

    for email_obj in emails:
        if get_thread_contact(email_obj) == contact:
            thread_emails.append(email_obj)

    # Auto-mark as read if account setting is on
    if store_id and thread_emails:
        account = EmailAccount.objects.filter(store_id=store_id, is_active=True).first()
        if account and account.auto_mark_read_on_open:
            unread_ids = [e.id for e in thread_emails if not e.is_read]
            if unread_ids:
                EmailMessage.objects.filter(id__in=unread_ids).update(is_read=True)
                for e in thread_emails:
                    e.is_read = True

    serializer = EmailMessageSerializer(thread_emails, many=True)

    return Response({
        "success": True,
        "contact": contact,
        "count": len(thread_emails),
        "emails": serializer.data
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def generate_ai_draft_api(request, email_id):
    email = get_object_or_404(EmailMessage, id=email_id)

    # Load tenant's email account to apply tone, custom instructions, signature
    account = EmailAccount.objects.filter(store=email.store, is_active=True).first()
    draft = generate_ai_reply(email, account=account)

    email.ai_draft = draft
    email.status = "drafted"
    email.save()

    return Response({
        "success": True,
        "ai_draft": draft
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def improve_reply_api(request):
    text = request.data.get("text", "").strip()

    if not text:
        return Response({
            "success": False,
            "message": "Text is required."
        }, status=400)

    prompt = f"""
You are a professional English editor.

Your job is ONLY to rewrite the given text.

STRICT RULES:
- Do NOT change the meaning
- Do NOT change the intent
- Do NOT change the type (question must stay question, statement must stay statement)
- Do NOT convert it into a customer support reply
- Do NOT add new information
- Do NOT assume context

You can:
- Fix grammar, spelling, punctuation
- Improve wording slightly
- Make it sound natural and professional

Keep it SHORT and CLOSE to original.

Original:
{text}

Corrected:
"""

    try:
        improved = generate_ai_text(prompt)

        if not improved or "AI suggestion failed" in improved:
            raise Exception("AI failed")

    except Exception:
        lower_text = text.lower()

        if "how are you" in lower_text or "whatsup" in lower_text or "hello" in lower_text or "helo" in lower_text:
            improved = "Hello, how are you?"

        elif "cancel" in lower_text and "order" in lower_text:
            improved = "Could you please share your order number so our team can check if your order can still be cancelled?"

        elif "order" in lower_text or "tracking" in lower_text:
            improved = "Could you please share your order number so we can check the latest tracking details for you?"

        elif "refund" in lower_text or "return" in lower_text:
            improved = "Could you please share your order number so our team can review your refund or return request?"

        elif "address" in lower_text:
            improved = "Could you please share the updated address details so our team can check if the order can still be updated?"

        else:
            improved = text.capitalize()

    return Response({
        "success": True,
        "suggestion": improved
    })


def _ai_get_user_store(request, store_id=None):
    """Resolve a store for the current user (used by AI Training endpoints)."""
    try:
        if store_id:
            return Store.objects.filter(id=store_id).first()
        if request.user.is_authenticated:
            return Store.objects.filter(user=request.user, is_active=True).first()
    except Exception:
        return None
    return None


@csrf_exempt
@api_view(["GET", "PUT"])
@authentication_classes([])
@permission_classes([AllowAny])
def ai_training_profile_api(request):
    """GET or PUT the AI Training Profile for the current user's active store."""
    from .models import AiTrainingProfile
    store = _ai_get_user_store(request, request.GET.get("store_id") or request.data.get("store_id"))
    if not store:
        return Response({"success": False, "message": "No active store found."}, status=400)

    profile, _ = AiTrainingProfile.objects.get_or_create(store=store)

    if request.method == "GET":
        return Response({
            "success": True,
            "profile": {
                "business_name":  profile.business_name,
                "niche":          profile.niche,
                "description":    profile.description,
                "language":       profile.language,
                "support_hours":  profile.support_hours,
                "tones":          profile.tones or [],
                "reply_length":   profile.reply_length,
                "signoff":        profile.signoff,
                "voice_example":  profile.voice_example,
                "mode":           profile.mode,
                "toggles":        profile.toggles or {},
                "wizard_answers": profile.wizard_answers or {},
                "wizard_completed_at": profile.wizard_completed_at.isoformat() if profile.wizard_completed_at else None,
                "extras":         profile.extras or {},
            }
        })

    # PUT
    data = request.data or {}
    for f in ["business_name", "niche", "description", "language", "support_hours",
              "reply_length", "signoff", "voice_example", "mode"]:
        if f in data:
            setattr(profile, f, data[f] or "")
    if "tones"          in data: profile.tones          = list(data["tones"] or [])
    if "toggles"        in data: profile.toggles        = dict(data["toggles"] or {})
    if "wizard_answers" in data:
        profile.wizard_answers = dict(data["wizard_answers"] or {})
        from django.utils import timezone as _tz
        profile.wizard_completed_at = _tz.now()
    if "extras"         in data: profile.extras         = dict(data["extras"] or {})
    profile.save()

    # Sync the AI mode to all EmailAccount(s) under this store so the
    # live email flow respects the wizard / studio setting.
    if "mode" in data:
        mode_map = {"auto": "auto", "draft": "suggest", "suggest": "suggest"}
        target_mode = mode_map.get(profile.mode, "suggest")
        try:
            from .models import EmailAccount
            EmailAccount.objects.filter(store=store).update(ai_reply_mode=target_mode)
        except Exception:
            pass

    return Response({"success": True, "message": "Profile saved."})


@csrf_exempt
@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def ai_training_snippets_api(request):
    """GET all snippets for current store. POST creates a new one."""
    from .models import KnowledgeSnippet
    store = _ai_get_user_store(request, request.GET.get("store_id") or request.data.get("store_id"))
    if not store:
        return Response({"success": False, "message": "No active store found."}, status=400)

    if request.method == "GET":
        snippets = KnowledgeSnippet.objects.filter(store=store)
        return Response({
            "success": True,
            "snippets": [{
                "id":          s.id,
                "category":    s.category,
                "title":       s.title,
                "text":        s.text,
                "from_wizard": s.from_wizard,
                "order_idx":   s.order_idx,
            } for s in snippets]
        })

    # POST
    title    = (request.data.get("title") or "").strip()
    text     = (request.data.get("text")  or "").strip()
    category = (request.data.get("category") or "FAQ").strip()
    if not title or not text:
        return Response({"success": False, "message": "title and text required."}, status=400)
    s = KnowledgeSnippet.objects.create(
        store=store,
        category=category,
        title=title,
        text=text,
        from_wizard=bool(request.data.get("from_wizard", False)),
        order_idx=int(request.data.get("order_idx", 0)),
    )
    return Response({
        "success": True,
        "snippet": {"id": s.id, "category": s.category, "title": s.title, "text": s.text,
                    "from_wizard": s.from_wizard, "order_idx": s.order_idx}
    })


@csrf_exempt
@api_view(["PUT", "DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
def ai_training_snippet_detail_api(request, snippet_id):
    """PUT updates, DELETE removes."""
    from .models import KnowledgeSnippet
    store = _ai_get_user_store(request, request.data.get("store_id") if hasattr(request, "data") else None)
    s = KnowledgeSnippet.objects.filter(id=snippet_id).first()
    if not s:
        return Response({"success": False, "message": "Snippet not found."}, status=404)
    # Permission: must belong to user's store
    if store and s.store_id != store.id:
        return Response({"success": False, "message": "Forbidden."}, status=403)

    if request.method == "DELETE":
        s.delete()
        return Response({"success": True, "message": "Deleted."})

    data = request.data or {}
    if "title"    in data: s.title    = data["title"]
    if "text"     in data: s.text     = data["text"]
    if "category" in data: s.category = data["category"]
    if "order_idx" in data: s.order_idx = int(data["order_idx"])
    s.save()
    return Response({"success": True, "message": "Updated."})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def ai_playground_api(request):
    """
    Generic Claude playground endpoint for AI Training Studio.

    Accepts:
      - system_prompt: assembled system prompt from frontend
      - user_message:  the customer email body
      - sender:        (optional) sender email — used for customer lookup
      - subject:       (optional) email subject — scanned for refs
      - auto_lookup:   (optional, default true) — auto-extract customer refs + lookup orders
      - store_id:      (optional) — scope lookup to a specific store

    Returns: { success, reply, detected_refs, matched_orders, final_system_prompt }
    """
    system_prompt = (request.data.get("system_prompt") or "").strip()
    user_message  = (request.data.get("user_message")  or "").strip()
    sender        = (request.data.get("sender")        or "").strip()
    subject       = (request.data.get("subject")       or "").strip()
    auto_lookup   = request.data.get("auto_lookup", True)
    store_id      = request.data.get("store_id")

    if not user_message:
        return Response({"success": False, "message": "user_message is required."}, status=400)

    detected_refs = []
    matched_orders = []
    context_block = ""

    try:
        from .services import (
            call_claude,
            extract_customer_refs,
            resolve_customer_context,
            build_context_block_for_prompt,
            serialize_order_for_ai,
        )

        if auto_lookup:
            # Extract from subject + body + sender
            scan_text = " ".join([subject, user_message, sender])
            detected_refs = extract_customer_refs(scan_text)

            # Scope to store if provided, else first store of user (best-effort)
            store = None
            if store_id:
                try:
                    store = Store.objects.filter(id=store_id).first()
                except Exception:
                    store = None
            if store is None and request.user.is_authenticated:
                store = Store.objects.filter(user=request.user, is_active=True).first()

            resolved = resolve_customer_context(detected_refs, sender_email=sender, store=store)
            orders = resolved.get("orders") or []
            matched_orders = [serialize_order_for_ai(o) for o in orders]
            context_block = build_context_block_for_prompt(orders)

        # Inject context into system prompt if anything found
        final_system = system_prompt or "You are a professional ecommerce customer support assistant."
        if context_block:
            final_system = f"{final_system}\n\n{context_block}"

        reply = call_claude(
            prompt=user_message,
            system=final_system,
            max_tokens=1024,
        )

        return Response({
            "success": True,
            "reply": reply,
            "detected_refs": detected_refs,
            "matched_orders": matched_orders,
            "final_system_prompt": final_system,
        })
    except Exception as e:
        return Response({"success": False, "message": str(e)}, status=500)


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def auto_suggest_reply_api(request):
    """Detect customer tone and return a tone-appropriate reply suggestion."""
    store_id = request.data.get("store_id")
    contact = extract_clean_email(request.data.get("contact", ""))

    if not store_id or not contact:
        return Response({"success": False, "message": "store_id and contact required."}, status=400)

    # Use same thread-matching logic as email_thread_detail_api
    emails = EmailMessage.objects.filter(store_id=store_id).order_by("created_at")
    thread_emails = [e for e in emails if get_thread_contact(e) == contact]

    if not thread_emails:
        return Response({"success": False, "message": "No messages found."}, status=404)

    # Get store email to identify customer vs support messages
    store_email = None
    try:
        account = EmailAccount.objects.filter(store_id=store_id, is_active=True).first()
        if account:
            store_email = extract_clean_email(account.email)
    except Exception:
        pass

    # Find most recent customer (non-support) message
    customer_msg = next(
        (e for e in reversed(thread_emails) if extract_clean_email(e.sender) != store_email),
        thread_emails[-1]
    )

    # Build brief conversation history (last 4 messages for context)
    history_lines = []
    for e in thread_emails[-4:]:
        role = "Support" if extract_clean_email(e.sender) == store_email else "Customer"
        snippet = (e.body or "")[:250].replace("\n", " ").strip()
        history_lines.append(f"{role}: {snippet}")
    history = "\n".join(history_lines)

    result = generate_smart_suggestion(customer_msg.body or "", history)

    if not result:
        return Response({"success": False, "message": "AI suggestion failed."}, status=500)

    return Response({
        "success": True,
        "tone": result["tone"],
        "tone_emoji": result["tone_emoji"],
        "reply": result["reply"],
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def send_email_reply_api(request, email_id):
    email = get_object_or_404(EmailMessage, id=email_id)

    reply_text = request.data.get("reply_text") or email.ai_draft

    if not reply_text:
        return Response({
            "success": False,
            "message": "Reply text is required."
        }, status=400)

    account = EmailAccount.objects.filter(store=email.store, is_active=True).first()

    # Append signature if set
    if account and account.signature:
        reply_text = reply_text.rstrip() + "\n\n--\n" + account.signature

    files = request.FILES.getlist("attachments")

    try:
        from_email = send_email_with_store_account(
            store=email.store,
            recipient=email.sender,
            subject=f"Re: {email.subject}",
            body=reply_text,
            files=files,
        )
    except Exception as e:
        return Response({
            "success": False,
            "message": str(e)
        }, status=400)

    reply_obj = EmailMessage.objects.create(
        store=email.store,
        sender=from_email,
        recipient=email.sender,
        subject=f"Re: {email.subject}",
        body=reply_text,
        status="replied",
        is_read=True,
        raw_data={
            "type": "outgoing",
            "source": "reply",
            "reply_to_email_id": email.id,
            "sent_from": from_email
        }
    )

    for f in files:
        f.seek(0)
        EmailAttachment.objects.create(
            email=reply_obj,
            filename=f.name,
            content_type=f.content_type,
            file=f,
            size=f.size,
        )

    email.status = "replied"
    email.is_read = True
    email.save()

    # Mark as read in Gmail IMAP if setting is on
    if account and account.mark_read_in_gmail and email.gmail_uid:
        from .services import mark_email_read_in_gmail
        mark_email_read_in_gmail(account, email.gmail_uid)

    # Auto-close thread if setting is on
    if account and account.auto_close_after_reply:
        contact_email = extract_clean_email(email.sender)
        EmailThreadAssignment.objects.filter(
            store=email.store,
            contact=contact_email
        ).update(is_resolved=True, resolved_at=timezone.now())

    return Response({
        "success": True,
        "message": "Email reply sent successfully.",
        "sent_from": from_email,
        "auto_closed": bool(account and account.auto_close_after_reply),
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def sync_inbox_api(request):
    store_id = request.data.get("store_id", 2)
    result = sync_gmail_inbox(store_id)

    return Response(result)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def download_attachment_api(_request, attachment_id):
    attachment = get_object_or_404(EmailAttachment, id=attachment_id)
    content_type = attachment.content_type or "application/octet-stream"
    if content_type.startswith("image/"):
        data = attachment.file.read()
        response = HttpResponse(data, content_type=content_type)
        response["Content-Disposition"] = f'inline; filename="{attachment.filename}"'
        return response
    return FileResponse(
        attachment.file.open("rb"),
        as_attachment=True,
        filename=attachment.filename,
    )


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def assign_thread_multi_api(request):
    """Assign multiple team members to a thread. First member becomes primary assigned_to."""
    from teamapp.models import TeamMember
    store_id = request.data.get("store_id")
    contact = (request.data.get("contact") or "").strip().lower()
    member_ids = request.data.get("member_ids", [])

    if not store_id or not contact:
        return Response({"success": False, "message": "store_id and contact are required."}, status=400)

    store = get_object_or_404(Store, id=store_id)
    members = list(TeamMember.objects.filter(id__in=member_ids))

    assignment, _ = EmailThreadAssignment.objects.get_or_create(store=store, contact=contact)
    assignment.assigned_to = members[0] if members else None
    assignment.save()
    assignment.co_assignees.set(members)

    assignees_data = [{"id": m.id, "name": m.name, "email": m.email, "role": m.role} for m in members]
    return Response({
        "success": True,
        "assigned_to": assignees_data[0] if assignees_data else None,
        "co_assignees": assignees_data,
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def assign_thread_api(request):
    from teamapp.models import TeamMember
    store_id = request.data.get("store_id")
    contact = (request.data.get("contact") or "").strip().lower()
    member_id = request.data.get("member_id")

    if not store_id or not contact or not member_id:
        return Response({"success": False, "message": "store_id, contact and member_id are required."}, status=400)

    store = get_object_or_404(Store, id=store_id)
    member = get_object_or_404(TeamMember, id=member_id)

    EmailThreadAssignment.objects.update_or_create(
        store=store,
        contact=contact,
        defaults={"assigned_to": member}
    )

    return Response({
        "success": True,
        "assigned_to": {
            "id": member.id,
            "name": member.name,
            "email": member.email,
            "role": member.role,
        }
    })


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def unassign_thread_api(request):
    store_id = request.data.get("store_id")
    contact = (request.data.get("contact") or "").strip().lower()

    if not store_id or not contact:
        return Response({"success": False, "message": "store_id and contact are required."}, status=400)

    EmailThreadAssignment.objects.filter(store_id=store_id, contact=contact).delete()
    return Response({"success": True})

# ─────────────────────────────────────────────
# 📋 EMAIL TEMPLATES
# ─────────────────────────────────────────────

def _template_to_dict(t, full=False):
    d = {
        'id': t.id,
        'name': t.name,
        'category': t.category,
        'status': t.status,
        'description': t.description,
        'tags': t.tags,
        'trigger_type': t.trigger_type,
        'subject': t.subject,
        'body_html': t.body_html,
        'is_category_default': t.is_category_default,
        'is_global': t.is_global,
        'updated_at': t.updated_at.isoformat(),
        'created_at': t.created_at.isoformat(),
    }
    if full:
        d.update({
            'preheader': t.preheader,
            'body_html': t.body_html,
            'footer': t.footer,
            'from_email': t.from_email or '',
            'sender_name': t.sender_name,
            'reply_to': t.reply_to or '',
            'cc_emails': t.cc_emails,
            'bcc_emails': t.bcc_emails,
            'use_default_signature': t.use_default_signature,
            'custom_signature': t.custom_signature,
            'trigger_delay_minutes': t.trigger_delay_minutes,
            'working_hours_only': t.working_hours_only,
            'throttle_per_day': t.throttle_per_day,
        })
    return d


def _apply_template_fields(t, data):
    fields = [
        'name', 'category', 'status', 'description', 'tags',
        'from_email', 'sender_name', 'reply_to', 'cc_emails', 'bcc_emails',
        'use_default_signature', 'custom_signature',
        'subject', 'preheader', 'body_html', 'footer',
        'trigger_type', 'trigger_delay_minutes', 'working_hours_only', 'throttle_per_day',
        'is_global',
    ]
    for f in fields:
        if f in data:
            val = data[f]
            if f in ('from_email', 'reply_to') and not val:
                val = None
            setattr(t, f, val)


@csrf_exempt
@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def email_templates_api(request):
    if request.method == "GET":
        store_id = request.GET.get("store_id")
        qs = EmailTemplate.objects.filter(
            Q(store_id=store_id) | Q(is_global=True)
        ).distinct().order_by('-is_category_default', 'id')
        if request.GET.get("category"):
            qs = qs.filter(category=request.GET["category"])
        if request.GET.get("status"):
            qs = qs.filter(status=request.GET["status"])
        return Response({'success': True, 'templates': [_template_to_dict(t) for t in qs], 'count': qs.count()})

    is_global = request.data.get("is_global", False)
    store_id = request.data.get("store_id")
    store = Store.objects.filter(id=store_id).first() if store_id else None
    if not store and not is_global:
        return Response({'success': False, 'message': 'Store not found.'}, status=404)
    t = EmailTemplate(store=store, is_global=bool(is_global))
    _apply_template_fields(t, request.data)
    t.save()
    return Response({'success': True, 'id': t.id, 'message': 'Template created.'})


@csrf_exempt
@api_view(["GET", "PUT", "DELETE"])
@authentication_classes([])
@permission_classes([AllowAny])
def email_template_detail_api(request, template_id):
    t = get_object_or_404(EmailTemplate, id=template_id)
    if request.method == "GET":
        return Response({'success': True, 'template': _template_to_dict(t, full=True)})
    if request.method == "PUT":
        _apply_template_fields(t, request.data)
        t.save()
        return Response({'success': True, 'message': 'Template saved.'})
    t.delete()
    return Response({'success': True, 'message': 'Template deleted.'})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def set_category_default_api(request, template_id):
    t = get_object_or_404(EmailTemplate, id=template_id)
    action = request.data.get("action", "set")  # "set" or "unset"
    force = request.data.get("force", False)

    if action == "unset":
        t.is_category_default = False
        t.save()
        return Response({'success': True, 'is_default': False})

    existing = EmailTemplate.objects.filter(
        store=t.store, category=t.category, is_category_default=True
    ).exclude(id=t.id).first()

    if existing and not force:
        return Response({
            'success': False,
            'conflict': True,
            'existing_name': existing.name,
            'existing_id': existing.id,
            'category_label': dict(EmailTemplate.CATEGORY_CHOICES).get(t.category, t.category),
        })

    EmailTemplate.objects.filter(
        store=t.store, category=t.category, is_category_default=True
    ).exclude(id=t.id).update(is_category_default=False)

    t.is_category_default = True
    t.save()
    return Response({'success': True, 'is_default': True})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def duplicate_template_api(request, template_id):
    t = get_object_or_404(EmailTemplate, id=template_id)
    new_t = EmailTemplate.objects.create(
        store=t.store, is_global=t.is_global, name=f'{t.name} (Copy)', category=t.category,
        status='draft', description=t.description, tags=t.tags,
        from_email=t.from_email, sender_name=t.sender_name, reply_to=t.reply_to,
        cc_emails=t.cc_emails, bcc_emails=t.bcc_emails,
        use_default_signature=t.use_default_signature, custom_signature=t.custom_signature,
        subject=t.subject, preheader=t.preheader, body_html=t.body_html, footer=t.footer,
        trigger_type=t.trigger_type, trigger_delay_minutes=t.trigger_delay_minutes,
        working_hours_only=t.working_hours_only, throttle_per_day=t.throttle_per_day,
    )
    return Response({'success': True, 'id': new_t.id})


@csrf_exempt
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def template_sample_data_api(request):
    store_id = request.GET.get("store_id")
    store = Store.objects.filter(id=store_id).first()
    ctx = build_template_context(store)
    return Response({'success': True, 'data': ctx})


@csrf_exempt
@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def send_test_template_api(request, template_id):
    t = get_object_or_404(EmailTemplate, id=template_id)
    test_email = (request.data.get('test_email') or '').strip()
    if not test_email:
        return Response({'success': False, 'message': 'Test email required.'}, status=400)

    ctx = build_template_context(t.store)

    subject = render_template_content(t.subject, ctx) or t.name
    body = render_template_content(t.body_html, ctx)
    footer_html = f'<div style="border-top:1px solid #e5e7eb;margin-top:24px;padding-top:16px;font-size:12px;color:#94a3b8;">{render_template_content(t.footer, ctx)}</div>' if t.footer else ''

    full_html = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;padding:0;background:#0f172a;">
{body}{footer_html}
</body></html>"""

    try:
        account = EmailAccount.objects.filter(store=t.store, is_active=True).first()
        if not account:
            return Response({'success': False, 'message': 'No active email account connected for this store.'}, status=400)

        send_email_with_store_account(
            store=t.store,
            recipient=test_email,
            subject=f'[TEST] {subject}',
            body=full_html,
        )
        return Response({'success': True, 'message': f'Test sent to {test_email} via {account.email}'})
    except Exception as e:
        return Response({'success': False, 'message': str(e)}, status=400)


# ─── Auto Email Toggle ────────────────────────────────────────────────────────

# ── Shared image block ─────────────────────────────────────────────────────────
_IMG = (
    '<div style="text-align:center;padding:24px 40px 0;">'
    '<img src="{{product_image}}" alt="Product" width="200"'
    ' style="max-width:200px;height:190px;object-fit:contain;display:block;margin:0 auto;'
    'border-radius:10px;" /></div>'
)

# ─ Style 1 · Shopify ──────────────────────────────────────────────────────────
def _shopify(badge, h1, body, cta, href="{{store_url}}", mid=""):
    return (
        '<div style="background:#f5f5f5;padding:32px 16px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.09);">'
        '<div style="background:#5c6ac4;padding:26px 40px;text-align:center;">'
        '<p style="margin:0;font-size:21px;font-weight:700;color:#fff;letter-spacing:-.2px;">{{store_name}}</p>'
        f'<p style="margin:5px 0 0;font-size:12px;color:rgba(255,255,255,.8);">{badge}</p>'
        '</div>'
        + _IMG +
        f'<div style="padding:28px 40px 36px;">'
        f'<h2 style="margin:0 0 8px;font-size:19px;font-weight:700;color:#212b36;">{h1}</h2>'
        f'<p style="margin:0 0 22px;font-size:14px;color:#637381;line-height:1.7;">{body}</p>'
        f'{mid}'
        f'<div style="text-align:center;margin-top:24px;">'
        f'<a href="{href}" style="display:inline-block;background:#5c6ac4;color:#fff;font-size:14px;font-weight:600;'
        f'text-decoration:none;padding:13px 34px;border-radius:50px;">{cta}</a></div>'
        '</div>'
        '<div style="border-top:1px solid #e5e7eb;padding:16px 40px;text-align:center;background:#fafafa;">'
        '<p style="margin:0;font-size:12px;color:#9ca3af;">'
        '<a href="{{store_url}}" style="color:#5c6ac4;text-decoration:none;">{{store_name}}</a> &nbsp;·&nbsp; '
        '<a href="mailto:{{store_email}}" style="color:#5c6ac4;text-decoration:none;">{{store_email}}</a>'
        '</p></div>'
        '</div></div>'
    )

# ─ Style 2 · Amazon ───────────────────────────────────────────────────────────
def _amazon(badge, h1, body, cta, mid=""):
    return (
        '<div style="background:#f3f3f3;padding:32px 16px;font-family:Arial,Helvetica,sans-serif;">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;border:1px solid #ddd;border-radius:4px;">'
        '<div style="background:#232f3e;padding:16px 24px;display:flex;align-items:center;">'
        '<p style="margin:0;font-size:22px;font-weight:700;color:#ff9900;">{{store_name}}</p>'
        f'<p style="margin:0 0 0 auto;font-size:12px;color:#aab7b8;white-space:nowrap;">{badge}</p>'
        '</div>'
        '<div style="padding:20px 24px 8px;background:#fff;text-align:center;border-bottom:1px solid #eee;">'
        '<img src="{{product_image}}" alt="Product" width="160"'
        ' style="max-width:160px;height:150px;object-fit:contain;display:inline-block;'
        'border:1px solid #ddd;padding:8px;border-radius:4px;background:#fff;" />'
        '</div>'
        f'<div style="padding:20px 24px 28px;">'
        f'<h2 style="margin:0 0 10px;font-size:18px;font-weight:700;color:#0f1111;">{h1}</h2>'
        f'<p style="margin:0 0 18px;font-size:14px;color:#565959;line-height:1.6;">{body}</p>'
        f'{mid}'
        '<div style="text-align:center;margin-top:20px;">'
        f'<a href="{{{{store_url}}}}" style="display:inline-block;background:#ff9900;color:#111;font-size:14px;'
        f'font-weight:700;text-decoration:none;padding:10px 28px;border-radius:3px;'
        f'border:1px solid #e47911;">{cta}</a></div>'
        '</div>'
        '<div style="border-top:1px solid #ddd;padding:14px 24px;text-align:center;background:#f7f7f7;">'
        '<p style="margin:0;font-size:12px;color:#888;">'
        'Need help? <a href="mailto:{{store_email}}" style="color:#0066c0;text-decoration:none;">{{store_email}}</a>'
        ' &nbsp;|&nbsp; <a href="{{store_url}}" style="color:#0066c0;text-decoration:none;">Visit Store</a>'
        '</p></div>'
        '</div></div>'
    )

# ─ Style 3 · Apple ────────────────────────────────────────────────────────────
def _apple(h1, body, cta, href="{{store_url}}", mid=""):
    return (
        '<div style="background:#fff;padding:40px 16px;font-family:-apple-system,BlinkMacSystemFont,\'SF Pro Text\',sans-serif;">'
        '<div style="max-width:560px;margin:0 auto;">'
        '<p style="margin:0 0 28px;text-align:center;font-size:13px;font-weight:600;color:#6e6e73;'
        'letter-spacing:.5px;text-transform:uppercase;">{{store_name}}</p>'
        '<div style="text-align:center;margin-bottom:28px;">'
        '<img src="{{product_image}}" alt="Product" width="220"'
        ' style="max-width:220px;height:200px;object-fit:contain;display:inline-block;" />'
        '</div>'
        '<hr style="border:none;border-top:1px solid #e5e5ea;margin:0 0 28px;" />'
        f'<h1 style="margin:0 0 14px;font-size:24px;font-weight:600;color:#1d1d1f;text-align:center;letter-spacing:-.3px;">{h1}</h1>'
        f'<p style="margin:0 0 24px;font-size:15px;color:#6e6e73;line-height:1.7;text-align:center;">{body}</p>'
        f'{mid}'
        '<div style="text-align:center;margin:28px 0;">'
        f'<a href="{href}" style="display:inline-block;background:#0071e3;color:#fff;font-size:15px;'
        f'font-weight:500;text-decoration:none;padding:12px 30px;border-radius:980px;">{cta}</a>'
        '</div>'
        '<hr style="border:none;border-top:1px solid #e5e5ea;margin:0 0 18px;" />'
        '<p style="margin:0;font-size:12px;color:#6e6e73;text-align:center;">'
        '{{store_name}} &nbsp;·&nbsp; '
        '<a href="mailto:{{store_email}}" style="color:#0071e3;text-decoration:none;">{{store_email}}</a>'
        '</p>'
        '</div></div>'
    )

# ─ Style 4 · Nike / Bold ──────────────────────────────────────────────────────
def _nike(badge, h1, body, cta, href="{{store_url}}", mid=""):
    return (
        '<div style="background:#f5f5f5;padding:32px 16px;font-family:\'Helvetica Neue\',Arial,sans-serif;">'
        '<div style="max-width:600px;margin:0 auto;background:#fff;overflow:hidden;">'
        '<div style="background:#111;padding:22px 32px;display:flex;align-items:center;justify-content:space-between;">'
        '<p style="margin:0;font-size:20px;font-weight:900;color:#fff;letter-spacing:-.5px;text-transform:uppercase;">{{store_name}}</p>'
        f'<span style="font-size:11px;font-weight:700;color:#e5e7eb;letter-spacing:1px;text-transform:uppercase;">{badge}</span>'
        '</div>'
        '<div style="background:#f5f5f5;text-align:center;padding:0;">'
        '<img src="{{product_image}}" alt="Product" width="100%"'
        ' style="width:100%;max-width:600px;height:260px;object-fit:contain;display:block;margin:0 auto;background:#f5f5f5;" />'
        '</div>'
        f'<div style="padding:28px 32px 36px;">'
        f'<h2 style="margin:0 0 10px;font-size:24px;font-weight:900;color:#111;letter-spacing:-.5px;text-transform:uppercase;">{h1}</h2>'
        f'<p style="margin:0 0 22px;font-size:14px;color:#555;line-height:1.7;">{body}</p>'
        f'{mid}'
        '<div style="margin-top:24px;">'
        f'<a href="{href}" style="display:inline-block;background:#111;color:#fff;font-size:14px;'
        f'font-weight:700;text-decoration:none;padding:14px 36px;letter-spacing:.5px;text-transform:uppercase;">{cta}</a>'
        '</div>'
        '</div>'
        '<div style="background:#111;padding:14px 32px;text-align:center;">'
        '<p style="margin:0;font-size:12px;color:#9ca3af;">'
        '<a href="{{store_url}}" style="color:#9ca3af;text-decoration:none;">{{store_name}}</a>'
        ' &nbsp;·&nbsp; '
        '<a href="mailto:{{store_email}}" style="color:#9ca3af;text-decoration:none;">{{store_email}}</a>'
        '</p></div>'
        '</div></div>'
    )

# ─ Style 5 · Luxury / Premium ─────────────────────────────────────────────────
def _luxury(badge, h1, body, cta, href="{{store_url}}", mid=""):
    return (
        '<div style="background:#faf8f5;padding:40px 16px;font-family:Georgia,\'Times New Roman\',serif;">'
        '<div style="max-width:580px;margin:0 auto;">'
        '<div style="text-align:center;padding-bottom:20px;border-bottom:1px solid #c9a96e;margin-bottom:28px;">'
        '<p style="margin:0;font-size:18px;font-weight:400;color:#1a1a1a;letter-spacing:3px;text-transform:uppercase;">{{store_name}}</p>'
        f'<p style="margin:6px 0 0;font-size:11px;color:#b8996e;letter-spacing:2px;text-transform:uppercase;">{badge}</p>'
        '</div>'
        '<div style="text-align:center;margin-bottom:28px;">'
        '<img src="{{product_image}}" alt="Product" width="200"'
        ' style="max-width:200px;height:190px;object-fit:contain;display:inline-block;'
        'border:1px solid #e8e0d5;padding:16px;background:#fff;" />'
        '</div>'
        f'<h2 style="margin:0 0 14px;font-size:22px;font-weight:400;color:#1a1a1a;text-align:center;letter-spacing:.5px;">{h1}</h2>'
        f'<p style="margin:0 0 24px;font-size:14px;color:#6b5f52;line-height:1.8;text-align:center;">{body}</p>'
        f'{mid}'
        '<div style="text-align:center;margin:28px 0;">'
        f'<a href="{href}" style="display:inline-block;background:transparent;color:#1a1a1a;'
        f'font-family:Georgia,serif;font-size:13px;font-weight:400;text-decoration:none;'
        f'padding:12px 32px;border:1px solid #1a1a1a;letter-spacing:2px;text-transform:uppercase;">{cta}</a>'
        '</div>'
        '<div style="border-top:1px solid #c9a96e;padding-top:18px;text-align:center;">'
        '<p style="margin:0;font-size:11px;color:#b8996e;letter-spacing:1px;text-transform:uppercase;">'
        '<a href="{{store_url}}" style="color:#b8996e;text-decoration:none;">{{store_name}}</a>'
        ' &nbsp;·&nbsp; '
        '<a href="mailto:{{store_email}}" style="color:#b8996e;text-decoration:none;">{{store_email}}</a>'
        '</p></div>'
        '</div></div>'
    )

# ── Shared content blocks ──────────────────────────────────────────────────────
def _order_tbl(accent="#5c6ac4"):
    return (
        '<table width="100%" cellpadding="0" cellspacing="0"'
        f' style="font-size:13px;border-collapse:collapse;margin-bottom:20px;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">'
        '<tr style="background:#f9fafb;"><th colspan="2" style="padding:9px 14px;font-size:11px;font-weight:700;'
        'color:#6b7280;letter-spacing:.6px;text-transform:uppercase;text-align:left;border-bottom:1px solid #e5e7eb;">Order Summary</th></tr>'
        '<tr><td style="padding:8px 14px;color:#6b7280;border-bottom:1px solid #f3f4f6;">Order</td>'
        '<td style="padding:8px 14px;color:#111;font-weight:600;text-align:right;border-bottom:1px solid #f3f4f6;">{{order_id}}</td></tr>'
        '<tr style="background:#fafafa;"><td style="padding:8px 14px;color:#6b7280;border-bottom:1px solid #f3f4f6;">Date</td>'
        '<td style="padding:8px 14px;color:#111;text-align:right;border-bottom:1px solid #f3f4f6;">{{order_date}}</td></tr>'
        '<tr><td style="padding:8px 14px;color:#6b7280;border-bottom:1px solid #f3f4f6;">Items</td>'
        '<td style="padding:8px 14px;color:#111;text-align:right;border-bottom:1px solid #f3f4f6;">{{order_items}}</td></tr>'
        '<tr style="background:#fafafa;"><td style="padding:8px 14px;color:#6b7280;border-bottom:1px solid #f3f4f6;">Shipping</td>'
        '<td style="padding:8px 14px;color:#111;text-align:right;border-bottom:1px solid #f3f4f6;">{{shipping_amount}}</td></tr>'
        f'<tr><td style="padding:10px 14px;font-weight:700;color:#111;">Total</td>'
        f'<td style="padding:10px 14px;font-weight:700;color:{accent};text-align:right;">{{{{order_total}}}}</td></tr>'
        '</table>'
    )

def _trk_tbl():
    return (
        '<table width="100%" cellpadding="0" cellspacing="0"'
        ' style="font-size:13px;border-collapse:collapse;margin-bottom:20px;border:1px solid #e5e7eb;border-radius:6px;overflow:hidden;">'
        '<tr style="background:#f9fafb;"><th colspan="2" style="padding:9px 14px;font-size:11px;font-weight:700;'
        'color:#6b7280;letter-spacing:.6px;text-transform:uppercase;text-align:left;border-bottom:1px solid #e5e7eb;">Tracking Details</th></tr>'
        '<tr><td style="padding:8px 14px;color:#6b7280;border-bottom:1px solid #f3f4f6;">Carrier</td>'
        '<td style="padding:8px 14px;color:#111;font-weight:600;text-align:right;border-bottom:1px solid #f3f4f6;">{{tracking_company}}</td></tr>'
        '<tr style="background:#fafafa;"><td style="padding:8px 14px;color:#6b7280;border-bottom:1px solid #f3f4f6;">Tracking #</td>'
        '<td style="padding:8px 14px;color:#111;font-weight:600;text-align:right;border-bottom:1px solid #f3f4f6;">{{tracking_number}}</td></tr>'
        '<tr><td style="padding:8px 14px;color:#6b7280;">Delivering To</td>'
        '<td style="padding:8px 14px;color:#111;text-align:right;">{{shipping_city}}, {{shipping_country}}</td></tr>'
        '</table>'
    )

def _addr_blk():
    return (
        '<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:12px 16px;margin-bottom:20px;">'
        '<p style="margin:0 0 4px;font-size:11px;font-weight:700;color:#9ca3af;letter-spacing:.6px;text-transform:uppercase;">Shipping To</p>'
        '<p style="margin:0;font-size:14px;color:#374151;line-height:1.6;">{{customer_name}}<br>{{shipping_full_address}}</p>'
        '</div>'
    )


_SHOPIFY_TEMPLATES = [

    # ═══════════════════════════════════════════════════════════════════
    # ORDER CONFIRMATION  ×5
    # ═══════════════════════════════════════════════════════════════════
    {
        "category": "order", "is_default": True,
        "name": "Order Confirmed — Shopify Style",
        "status": "active", "trigger_type": "order_placed",
        "subject": "Your order {{order_id}} is confirmed ✓",
        "preheader": "Thank you! Here's your full order summary.",
        "body_html": _shopify(
            "Order Confirmed",
            "Thank you, {{customer_name}}! 🎉",
            "Your order has been placed and is now being processed. We'll send you a shipping update as soon as it's on its way.",
            "Continue Shopping",
            mid=_order_tbl("#5c6ac4") + _addr_blk(),
        ),
    },
    {
        "category": "order", "is_default": False,
        "name": "Order Confirmed — Amazon Style",
        "status": "active", "trigger_type": "order_placed",
        "subject": "Order {{order_id}} received — estimated delivery coming soon",
        "preheader": "We received your order and it's being processed.",
        "body_html": _amazon(
            "Order Received",
            "Thank you for your order, {{customer_name}}.",
            "Order <strong>{{order_id}}</strong> placed on <strong>{{order_date}}</strong> is now being processed. You will receive a shipping confirmation email with tracking details once your package ships.",
            "View Order",
            mid=_order_tbl("#ff9900") + _addr_blk(),
        ),
    },
    {
        "category": "order", "is_default": False,
        "name": "Order Confirmed — Apple Style",
        "status": "active", "trigger_type": "order_placed",
        "subject": "Your order is confirmed.",
        "preheader": "Here's a summary of what you ordered.",
        "body_html": _apple(
            "Your order is confirmed.",
            "Hi {{customer_name}}, thank you for your purchase. Order {{order_id}} placed on {{order_date}} is being prepared. We'll be in touch when it ships.",
            "View Store",
            mid=_order_tbl("#0071e3") + _addr_blk(),
        ),
    },
    {
        "category": "order", "is_default": False,
        "name": "Order Confirmed — Bold Style",
        "status": "active", "trigger_type": "order_placed",
        "subject": "Order received — we're on it, {{customer_name}}!",
        "preheader": "Your order is confirmed and being prepared.",
        "body_html": _nike(
            "Order Confirmed",
            "We Got Your Order.",
            "Hi {{customer_name}} — order <strong>{{order_id}}</strong> is confirmed and our team is already on it. You'll get a shipping notification as soon as your package leaves our warehouse.",
            "Shop More",
            mid=_order_tbl("#111") + _addr_blk(),
        ),
    },
    {
        "category": "order", "is_default": False,
        "name": "Order Confirmed — Luxury Style",
        "status": "active", "trigger_type": "order_placed",
        "subject": "Your order {{order_id}} — confirmed",
        "preheader": "Thank you for choosing us.",
        "body_html": _luxury(
            "Order Confirmation",
            "Thank You, {{customer_name}}",
            "We are delighted to confirm your order {{order_id}} placed on {{order_date}}. Your selection has been received and is being carefully prepared.",
            "Explore More",
            mid=_order_tbl("#b8996e") + _addr_blk(),
        ),
    },

    # ═══════════════════════════════════════════════════════════════════
    # SHIPPING NOTIFICATION  ×5
    # ═══════════════════════════════════════════════════════════════════
    {
        "category": "shipping", "is_default": True,
        "name": "Shipped — Shopify Style",
        "status": "active", "trigger_type": "tracking_added",
        "subject": "Your order {{order_id}} is on its way 🚚",
        "preheader": "Great news — your package has shipped!",
        "body_html": _shopify(
            "Shipment Update",
            "It's on its way, {{customer_name}}!",
            "Your order {{order_id}} has been shipped and is heading to <strong>{{shipping_city}}, {{shipping_country}}</strong>. Use the tracking details below to follow your package.",
            "Track Your Package",
            href="{{tracking_link}}",
            mid=_trk_tbl(),
        ),
    },
    {
        "category": "shipping", "is_default": False,
        "name": "Shipped — Amazon Style",
        "status": "active", "trigger_type": "tracking_added",
        "subject": "Your {{store_name}} order has shipped",
        "preheader": "Track your package anytime.",
        "body_html": _amazon(
            "Package Shipped",
            "Your package is on its way.",
            "Order <strong>{{order_id}}</strong> has been shipped via <strong>{{tracking_company}}</strong>. Tracking number: <strong>{{tracking_number}}</strong>. Delivering to <strong>{{shipping_city}}, {{shipping_country}}</strong>.",
            "Track Package",
            mid=_trk_tbl(),
        ),
    },
    {
        "category": "shipping", "is_default": False,
        "name": "Shipped — Apple Style",
        "status": "active", "trigger_type": "tracking_added",
        "subject": "Your order has shipped.",
        "preheader": "Track your package with the details inside.",
        "body_html": _apple(
            "Your order has shipped.",
            "Hi {{customer_name}}, your order {{order_id}} is on its way to {{shipping_city}}. You can track it anytime using the information below.",
            "Track Package",
            href="{{tracking_link}}",
            mid=_trk_tbl(),
        ),
    },
    {
        "category": "shipping", "is_default": False,
        "name": "Shipped — Bold Style",
        "status": "active", "trigger_type": "tracking_added",
        "subject": "🚚 Your package is moving, {{customer_name}}",
        "preheader": "Order {{order_id}} handed to the courier.",
        "body_html": _nike(
            "Shipped",
            "Your Order Is Moving.",
            "Order <strong>{{order_id}}</strong> has been handed to <strong>{{tracking_company}}</strong> and is on its way to you. Tracking number: <strong>{{tracking_number}}</strong>.",
            "Track Now",
            href="{{tracking_link}}",
            mid=_trk_tbl(),
        ),
    },
    {
        "category": "shipping", "is_default": False,
        "name": "Shipped — Luxury Style",
        "status": "active", "trigger_type": "tracking_added",
        "subject": "Your order {{order_id}} has been dispatched",
        "preheader": "Your package is on its way to you.",
        "body_html": _luxury(
            "Shipment Dispatched",
            "Your Order Is On Its Way",
            "Dear {{customer_name}}, your order {{order_id}} has been carefully dispatched via {{tracking_company}} and is on its way to {{shipping_city}}.",
            "Track Delivery",
            href="{{tracking_link}}",
            mid=_trk_tbl(),
        ),
    },

    # ═══════════════════════════════════════════════════════════════════
    # DELIVERED  ×5
    # ═══════════════════════════════════════════════════════════════════
    {
        "category": "refund", "is_default": True,
        "name": "Delivered — Shopify Style",
        "status": "active", "trigger_type": "order_delivered",
        "subject": "Your order {{order_id}} has been delivered 📦",
        "preheader": "We hope you love your purchase!",
        "body_html": _shopify(
            "Delivered",
            "Your order has arrived, {{customer_name}}!",
            "Order {{order_id}} was successfully delivered to <strong>{{shipping_city}}</strong>. We hope you love it! If there's any issue, just reach out and we'll fix it right away.",
            "Contact Support",
            href="mailto:{{store_email}}",
            mid='<div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:14px 18px;margin-bottom:20px;text-align:center;">'
                '<p style="margin:0;font-size:14px;font-weight:600;color:#16a34a;">✓ Successfully Delivered</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#6b7280;">{{tracking_number}}</p></div>',
        ),
    },
    {
        "category": "refund", "is_default": False,
        "name": "Delivered — Amazon Style",
        "status": "active", "trigger_type": "order_delivered",
        "subject": "Your package was delivered",
        "preheader": "Order {{order_id}} — delivered successfully.",
        "body_html": _amazon(
            "Delivered",
            "Your package has been delivered.",
            "Order <strong>{{order_id}}</strong> was delivered to <strong>{{shipping_city}}</strong>. Tracking number: <strong>{{tracking_number}}</strong>. We hope you enjoy your purchase!",
            "Leave a Review",
            mid='<div style="background:#f0fdf4;border:1px solid #ccc;padding:10px 16px;margin-bottom:16px;border-left:4px solid #16a34a;">'
                '<p style="margin:0;font-size:13px;color:#0f1111;font-weight:700;">✓ Package delivered to {{shipping_city}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#565959;">Tracking: {{tracking_number}}</p></div>',
        ),
    },
    {
        "category": "refund", "is_default": False,
        "name": "Delivered — Apple Style",
        "status": "active", "trigger_type": "order_delivered",
        "subject": "Your order has been delivered.",
        "preheader": "We hope you enjoy your purchase.",
        "body_html": _apple(
            "Your order was delivered.",
            "Hi {{customer_name}}, order {{order_id}} was delivered to {{shipping_city}}. We hope everything arrived perfectly. If not, we're here to help.",
            "Contact Support",
            href="mailto:{{store_email}}",
            mid='<div style="background:#f5f5f7;border-radius:10px;padding:16px;margin-bottom:22px;text-align:center;">'
                '<p style="margin:0;font-size:14px;font-weight:500;color:#1d1d1f;">✓ Delivered — {{tracking_number}}</p></div>',
        ),
    },
    {
        "category": "refund", "is_default": False,
        "name": "Delivered — Bold Style",
        "status": "active", "trigger_type": "order_delivered",
        "subject": "Delivered. Enjoy it, {{customer_name}}! 📦",
        "preheader": "Your order {{order_id}} is in your hands.",
        "body_html": _nike(
            "Delivered",
            "It's Yours Now.",
            "Order <strong>{{order_id}}</strong> has been delivered to <strong>{{shipping_city}}</strong>. Unbox it, use it, love it. Any issues? We've got you covered.",
            "Contact Us",
            href="mailto:{{store_email}}",
            mid='<div style="background:#f5f5f5;border-left:4px solid #16a34a;padding:12px 16px;margin-bottom:20px;">'
                '<p style="margin:0;font-size:14px;font-weight:700;color:#111;">✓ DELIVERED TO {{shipping_city}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#555;">{{tracking_number}}</p></div>',
        ),
    },
    {
        "category": "refund", "is_default": False,
        "name": "Delivered — Luxury Style",
        "status": "active", "trigger_type": "order_delivered",
        "subject": "Your order {{order_id}} has arrived",
        "preheader": "Delivered with care.",
        "body_html": _luxury(
            "Delivered",
            "Your Order Has Arrived",
            "Dear {{customer_name}}, we are pleased to confirm that your order {{order_id}} has been delivered to {{shipping_city}}. We hope it brings you great satisfaction.",
            "Share Your Experience",
            mid='<div style="border:1px solid #c9a96e;padding:14px 20px;margin-bottom:24px;text-align:center;">'
                '<p style="margin:0;font-size:13px;color:#b8996e;letter-spacing:1px;text-transform:uppercase;">✓ Delivered — {{tracking_number}}</p></div>',
        ),
    },

    # ═══════════════════════════════════════════════════════════════════
    # ORDER CANCELLED  ×5
    # ═══════════════════════════════════════════════════════════════════
    {
        "category": "cancelled", "is_default": True,
        "name": "Cancelled — Shopify Style",
        "status": "active", "trigger_type": "order_cancelled",
        "subject": "Your order {{order_id}} has been cancelled",
        "preheader": "Your refund will be processed within 5–7 business days.",
        "body_html": _shopify(
            "Order Cancelled",
            "Order {{order_id}} Cancelled",
            "Hi {{customer_name}}, your order has been cancelled. Your refund of <strong>{{order_total}}</strong> will be returned to your original payment method within 5–7 business days.",
            "Continue Shopping",
            mid='<div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:14px 18px;margin-bottom:20px;">'
                '<table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;">'
                '<tr><td style="color:#6b7280;padding-bottom:8px;">Order</td><td style="color:#111;font-weight:600;text-align:right;padding-bottom:8px;">{{order_id}}</td></tr>'
                '<tr><td style="color:#6b7280;padding-bottom:8px;">Date</td><td style="color:#111;text-align:right;padding-bottom:8px;">{{order_date}}</td></tr>'
                '<tr><td style="color:#6b7280;border-top:1px solid #e5e7eb;padding-top:8px;font-weight:700;">Refund</td>'
                '<td style="color:#16a34a;font-weight:700;text-align:right;border-top:1px solid #e5e7eb;padding-top:8px;">{{order_total}}</td></tr>'
                '</table></div>',
        ),
    },
    {
        "category": "cancelled", "is_default": False,
        "name": "Cancelled — Amazon Style",
        "status": "active", "trigger_type": "order_cancelled",
        "subject": "Your order {{order_id}} has been cancelled",
        "preheader": "Refund processing details inside.",
        "body_html": _amazon(
            "Cancellation Confirmed",
            "Your order has been cancelled.",
            "Hi {{customer_name}}, order <strong>{{order_id}}</strong> ({{order_date}}) has been cancelled. A refund of <strong>{{order_total}}</strong> will be credited to your {{payment_method}} within 5–7 business days.",
            "Browse Again",
            mid='<div style="background:#fff8e6;border:1px solid #ff9900;border-left:4px solid #ff9900;padding:10px 14px;margin-bottom:16px;">'
                '<p style="margin:0;font-size:13px;color:#0f1111;font-weight:700;">Refund: {{order_total}} → {{payment_method}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#565959;">Allow 5–7 business days to process.</p></div>',
        ),
    },
    {
        "category": "cancelled", "is_default": False,
        "name": "Cancelled — Apple Style",
        "status": "active", "trigger_type": "order_cancelled",
        "subject": "Your order has been cancelled.",
        "preheader": "A refund is on its way.",
        "body_html": _apple(
            "Your order has been cancelled.",
            "Hi {{customer_name}}, order {{order_id}} has been cancelled. Your refund of {{order_total}} will be returned to your {{payment_method}} within 5–7 business days.",
            "Return to Store",
            mid='<div style="background:#f5f5f7;border-radius:10px;padding:16px;margin-bottom:22px;text-align:center;">'
                '<p style="margin:0;font-size:14px;font-weight:500;color:#1d1d1f;">Refund: {{order_total}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#6e6e73;">5–7 business days to your {{payment_method}}</p></div>',
        ),
    },
    {
        "category": "cancelled", "is_default": False,
        "name": "Cancelled — Bold Style",
        "status": "active", "trigger_type": "order_cancelled",
        "subject": "Order {{order_id}} cancelled",
        "preheader": "Your refund is being processed.",
        "body_html": _nike(
            "Cancelled",
            "Order Cancelled.",
            "Hi {{customer_name}}, order <strong>{{order_id}}</strong> has been cancelled. Refund of <strong>{{order_total}}</strong> will be processed to {{payment_method}} within 5–7 business days.",
            "Shop Again",
            mid='<div style="background:#f5f5f5;border-left:4px solid #16a34a;padding:12px 16px;margin-bottom:20px;">'
                '<p style="margin:0;font-size:14px;font-weight:700;color:#111;">REFUND: {{order_total}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#555;">Returns to {{payment_method}} in 5–7 days.</p></div>',
        ),
    },
    {
        "category": "cancelled", "is_default": False,
        "name": "Cancelled — Luxury Style",
        "status": "active", "trigger_type": "order_cancelled",
        "subject": "Cancellation confirmed — order {{order_id}}",
        "preheader": "Your refund is being arranged.",
        "body_html": _luxury(
            "Cancellation Confirmed",
            "We're Sorry to See You Go",
            "Dear {{customer_name}}, we confirm the cancellation of order {{order_id}}. A full refund of {{order_total}} will be returned to your {{payment_method}} within 5–7 business days.",
            "Visit Our Store",
            mid='<div style="border:1px solid #c9a96e;padding:14px 20px;margin-bottom:24px;text-align:center;">'
                '<p style="margin:0;font-size:13px;color:#b8996e;letter-spacing:1px;text-transform:uppercase;">Refund: {{order_total}} — In Progress</p></div>',
        ),
    },

    # ═══════════════════════════════════════════════════════════════════
    # PAYMENT FAILED  ×5
    # ═══════════════════════════════════════════════════════════════════
    {
        "category": "failed", "is_default": True,
        "name": "Payment Failed — Shopify Style",
        "status": "active", "trigger_type": "payment_failed",
        "subject": "Action required: Payment failed for order {{order_id}}",
        "preheader": "Please update your payment details to keep your order.",
        "body_html": _shopify(
            "Payment Failed",
            "We couldn't process your payment.",
            "Hi {{customer_name}}, the payment for order {{order_id}} ({{order_total}}) via <strong>{{payment_method}}</strong> could not be completed. Please update your payment details to avoid losing your order.",
            "Update Payment Details",
            mid='<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;padding:12px 16px;margin-bottom:20px;">'
                '<p style="margin:0;font-size:13px;color:#dc2626;font-weight:600;">⚠ Payment of {{order_total}} declined via {{payment_method}}</p></div>',
        ),
    },
    {
        "category": "failed", "is_default": False,
        "name": "Payment Failed — Amazon Style",
        "status": "active", "trigger_type": "payment_failed",
        "subject": "Action required — payment issue on order {{order_id}}",
        "preheader": "Update your payment method to complete your order.",
        "body_html": _amazon(
            "Payment Issue",
            "We were unable to process your payment.",
            "Hi {{customer_name}}, we attempted to charge <strong>{{order_total}}</strong> to your {{payment_method}} for order <strong>{{order_id}}</strong>, but the transaction was declined. Please update your payment information as soon as possible.",
            "Update Payment",
            mid='<div style="background:#fff8e6;border:1px solid #ff9900;border-left:4px solid #e53e00;padding:10px 14px;margin-bottom:16px;">'
                '<p style="margin:0;font-size:13px;color:#0f1111;font-weight:700;">⚠ Declined: {{order_total}} via {{payment_method}}</p></div>',
        ),
    },
    {
        "category": "failed", "is_default": False,
        "name": "Payment Failed — Apple Style",
        "status": "active", "trigger_type": "payment_failed",
        "subject": "There was an issue with your payment.",
        "preheader": "Please update your payment method.",
        "body_html": _apple(
            "There was an issue with your payment.",
            "Hi {{customer_name}}, we weren't able to process payment of {{order_total}} for order {{order_id}}. Please update your payment details to keep your order active.",
            "Update Payment",
            mid='<div style="background:#fff2f2;border-radius:10px;padding:16px;margin-bottom:22px;text-align:center;">'
                '<p style="margin:0;font-size:14px;font-weight:500;color:#dc2626;">Payment declined: {{order_total}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#6e6e73;">{{payment_method}}</p></div>',
        ),
    },
    {
        "category": "failed", "is_default": False,
        "name": "Payment Failed — Bold Style",
        "status": "active", "trigger_type": "payment_failed",
        "subject": "⚠ Payment failed — order {{order_id}} at risk",
        "preheader": "Act now to save your order.",
        "body_html": _nike(
            "Action Required",
            "Payment Failed.",
            "Hi {{customer_name}}, your payment of <strong>{{order_total}}</strong> for order <strong>{{order_id}}</strong> via {{payment_method}} was declined. Update your payment now before your order is cancelled.",
            "Fix Payment Now",
            mid='<div style="background:#fee2e2;border-left:4px solid #dc2626;padding:12px 16px;margin-bottom:20px;">'
                '<p style="margin:0;font-size:14px;font-weight:700;color:#dc2626;">⚠ DECLINED: {{order_total}}</p>'
                '<p style="margin:4px 0 0;font-size:12px;color:#555;">{{payment_method}}</p></div>',
        ),
    },
    {
        "category": "failed", "is_default": False,
        "name": "Payment Failed — Luxury Style",
        "status": "active", "trigger_type": "payment_failed",
        "subject": "Payment issue on your order {{order_id}}",
        "preheader": "Please review your payment details.",
        "body_html": _luxury(
            "Payment Notice",
            "We Were Unable to Process Your Payment",
            "Dear {{customer_name}}, we regret to inform you that the payment of {{order_total}} for order {{order_id}} via {{payment_method}} could not be processed. Please update your payment information at your earliest convenience.",
            "Update Payment",
            mid='<div style="border:1px solid #dc2626;padding:14px 20px;margin-bottom:24px;text-align:center;">'
                '<p style="margin:0;font-size:13px;color:#dc2626;letter-spacing:.5px;">Payment Declined: {{order_total}} via {{payment_method}}</p></div>',
        ),
    },

    # ═══════════════════════════════════════════════════════════════════
    # FOLLOW-UP  ×5
    # ═══════════════════════════════════════════════════════════════════
    {
        "category": "followup", "is_default": True,
        "name": "Follow-up — Shopify Style",
        "status": "active", "trigger_type": "no_activity_7d",
        "subject": "How was your experience with {{store_name}}?",
        "preheader": "We'd love to hear your feedback!",
        "body_html": _shopify(
            "We'd Love Your Feedback",
            "How did we do, {{customer_name}}?",
            "It's been a little while since your order {{order_id}} arrived. We hope you love it! Leaving a quick review helps us improve and helps other customers make great choices.",
            "Leave a Review",
        ),
    },
    {
        "category": "followup", "is_default": False,
        "name": "Follow-up — Amazon Style",
        "status": "active", "trigger_type": "no_activity_7d",
        "subject": "How is your order {{order_id}}? Share your review",
        "preheader": "Your feedback helps thousands of customers.",
        "body_html": _amazon(
            "Rate Your Purchase",
            "How would you rate your recent purchase?",
            "Hi {{customer_name}}, it's been a while since your order <strong>{{order_id}}</strong> was delivered. We'd love to hear what you think! Your review helps other customers and helps us serve you better.",
            "Write a Review",
            mid='<div style="text-align:center;font-size:24px;margin:0 0 14px;letter-spacing:4px;color:#ff9900;">★★★★★</div>',
        ),
    },
    {
        "category": "followup", "is_default": False,
        "name": "Follow-up — Apple Style",
        "status": "active", "trigger_type": "no_activity_7d",
        "subject": "Tell us about your experience.",
        "preheader": "How are you enjoying your purchase?",
        "body_html": _apple(
            "How are you enjoying it?",
            "Hi {{customer_name}}, it's been a little while since order {{order_id}} arrived. We'd love to know how you're finding it. Your feedback means a lot to us.",
            "Share Feedback",
            mid='<div style="background:#f5f5f7;border-radius:10px;padding:16px;margin-bottom:22px;text-align:center;">'
                '<p style="margin:0;font-size:22px;letter-spacing:6px;color:#0071e3;">★★★★★</p>'
                '<p style="margin:8px 0 0;font-size:13px;color:#6e6e73;">How would you rate your order?</p></div>',
        ),
    },
    {
        "category": "followup", "is_default": False,
        "name": "Follow-up — Bold Style",
        "status": "active", "trigger_type": "no_activity_7d",
        "subject": "Still loving your order {{order_id}}? Tell us!",
        "preheader": "Your feedback drives us forward.",
        "body_html": _nike(
            "Your Opinion Matters",
            "How Are You Finding It?",
            "Hi {{customer_name}}, order <strong>{{order_id}}</strong> has been with you for a while now. We want to know — are you loving it? Your honest feedback pushes us to be better.",
            "Leave a Review",
            mid='<div style="background:#f5f5f5;padding:14px 16px;margin-bottom:20px;text-align:center;">'
                '<p style="margin:0;font-size:20px;letter-spacing:4px;color:#111;">★★★★★</p></div>',
        ),
    },
    {
        "category": "followup", "is_default": False,
        "name": "Follow-up — Luxury Style",
        "status": "active", "trigger_type": "no_activity_7d",
        "subject": "We hope you are enjoying your purchase — {{store_name}}",
        "preheader": "Your satisfaction is our priority.",
        "body_html": _luxury(
            "Your Satisfaction",
            "We Hope You Are Delighted",
            "Dear {{customer_name}}, it has been some time since the delivery of order {{order_id}}. We sincerely hope your experience has been exceptional. We would be honoured if you would share your thoughts.",
            "Share Your Experience",
            mid='<div style="border:1px solid #c9a96e;padding:14px 20px;margin-bottom:24px;text-align:center;">'
                '<p style="margin:0;font-size:18px;letter-spacing:6px;color:#b8996e;">★★★★★</p></div>',
        ),
    },
]


def _seed_shopify_templates(store_id):
    from emails.default_templates import PORTAL_DEFAULT_TEMPLATES
    from itertools import groupby
    sorted_tpls = sorted(PORTAL_DEFAULT_TEMPLATES, key=lambda t: t["category"])
    for cat, group in groupby(sorted_tpls, key=lambda t: t["category"]):
        if EmailTemplate.objects.filter(store_id=store_id, category=cat).exists():
            continue
        bulk = []
        for tpl in group:
            bulk.append(EmailTemplate(
                store_id=store_id,
                **{k: tpl.get(k) for k in [
                    'name', 'category', 'status', 'subject', 'preheader', 'body_html',
                    'trigger_type', 'trigger_delay_minutes', 'working_hours_only',
                    'throttle_per_day', 'is_category_default', 'is_global',
                    'description', 'tags', 'from_email', 'sender_name', 'reply_to',
                    'cc_emails', 'bcc_emails', 'use_default_signature', 'custom_signature', 'footer',
                ]}
            ))
        EmailTemplate.objects.bulk_create(bulk)


def _active_categories_for_store(store_id):
    return list(
        EmailTemplate.objects.filter(
            is_category_default=True, status="active"
        ).filter(
            Q(store_id=store_id) | Q(is_global=True)
        ).values_list("category", flat=True)
    )


@csrf_exempt
@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def auto_email_toggle_api(request):
    store_id = request.data.get("store_id") or request.GET.get("store_id")
    account = EmailAccount.objects.filter(store_id=store_id).first()

    if request.method == "GET":
        enabled = account.auto_email_enabled if account else False
        return Response({
            "success": True,
            "auto_email_enabled": enabled,
            "active_categories": _active_categories_for_store(store_id),
        })

    # POST — toggle
    enabled = request.data.get("enabled")
    if enabled is None:
        enabled = not (account.auto_email_enabled if account else False)
    enabled = bool(enabled)

    # Seed default templates the very first time Auto Email is turned ON
    # (works even without an email account connected)
    if enabled and not EmailTemplate.objects.filter(store_id=store_id).exists():
        _seed_shopify_templates(store_id)

    if not account:
        # No email account — templates seeded but auto-send can't be enabled
        return Response({
            "success": False,
            "seeded": True,
            "message": "Templates created! Connect an email account to enable auto-sending.",
            "active_categories": _active_categories_for_store(store_id),
        }, status=400)

    account.auto_email_enabled = enabled
    if enabled and not account.templates_seeded:
        account.templates_seeded = True
        account.save(update_fields=["auto_email_enabled", "templates_seeded"])
    else:
        account.save(update_fields=["auto_email_enabled"])

    return Response({
        "success": True,
        "auto_email_enabled": account.auto_email_enabled,
        "active_categories": _active_categories_for_store(store_id),
    })


# ─── Auto-send email on order status change ───────────────────────────────────

STATUS_TO_CATEGORY = {
    "processing": "order",
    "pending":    "order",
    "on-hold":    "order",
    "shipped":    "shipping",
    "shipping":   "shipping",
    "in transit": "shipping",
    "in_transit": "shipping",
    "completed":  "followup",
    "delivered":  "followup",
    "failed":     "failed",
    "cancelled":  "cancelled",
    "canceled":   "cancelled",
    "dispute":    "dispute",
    "refunded":   "refund",
}

def send_auto_status_email(order, new_status):
    """Call this whenever an order's fulfillment_status changes."""
    try:
        account = EmailAccount.objects.filter(store=order.store, is_active=True).first()
        if not account or not account.auto_email_enabled:
            return
        category = STATUS_TO_CATEGORY.get((new_status or "").lower().strip())
        if not category:
            return
        template = EmailTemplate.objects.filter(
            is_category_default=True,
            status="active",
            category=category,
        ).filter(
            Q(store=order.store) | Q(is_global=True)
        ).first()
        if not template:
            return
        recipient = order.customer_email
        if not recipient:
            return
        # Pass the specific triggered order so context contains correct data
        context = build_template_context(order.store, order=order)
        subject = render_template_content(template.subject or f"Order Update: {new_status.title()}", context)
        body_html = render_template_content(template.body_html, context)
        send_email_with_store_account(order.store, recipient, subject, body_html)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"send_auto_status_email failed: {e}", exc_info=True)
