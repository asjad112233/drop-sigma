import json
import os
import threading
import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.models import User
from django.contrib.auth import login
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.utils import timezone
from django.db.models import Count, Sum, Q
import resend as _resend

from stores.models import Store
from orders.models import Order
from vendors.models import Vendor
from teamapp.models import TeamMember

from .models import Tenant, Subscription, TenantActivity, PLAN_PRICES, Coupon


# ── Email Templates ───────────────────────────────────────────────────────────

_LOGO = """<table cellpadding="0" cellspacing="0"><tr>
  <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:14px;width:44px;height:44px;text-align:center;vertical-align:middle;">
    <span style="color:#fff;font-weight:900;font-size:17px;letter-spacing:-.5px;">DS</span>
  </td>
  <td style="padding-left:12px;text-align:left;">
    <div style="font-size:19px;font-weight:900;color:#0f172a;letter-spacing:-.4px;">Drop Sigma</div>
    <div style="font-size:11px;color:#94a3b8;margin-top:1px;">Ecommerce Operations OS</div>
  </td>
</tr></table>"""

_FOOTER = """<p style="margin:0 0 6px;font-size:12px;color:#94a3b8;">
  &copy; 2026 Drop Sigma &nbsp;&middot;&nbsp;
  <a href="https://dropsigma.com" style="color:#94a3b8;text-decoration:none;">dropsigma.com</a>
  &nbsp;&middot;&nbsp;
  <a href="mailto:support@dropsigma.com" style="color:#94a3b8;text-decoration:none;">support@dropsigma.com</a>
</p>
<p style="margin:0;font-size:11px;color:#cbd5e1;">You received this because you have a Drop Sigma account.</p>"""


def _build_suspend_email(name, reason):
    reason_text = reason or "Policy violation"
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#ef4444,#f97316);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#dc2626;background:#fee2e2;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x26A0;&#xFE0F; ACCOUNT SUSPENDED</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Your account has been suspended</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, your Drop Sigma account has been temporarily suspended by our team.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td style="padding:24px 48px;">
      <div style="background:#fef2f2;border:1px solid #fecaca;border-left:4px solid #ef4444;border-radius:10px;padding:18px 20px;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#dc2626;text-transform:uppercase;letter-spacing:0.5px;">Reason</p>
        <p style="margin:0;font-size:14px;color:#7f1d1d;line-height:1.6;">{reason_text}</p>
      </div>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 14px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">What this means</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x274C; &nbsp;Dashboard access is disabled</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x274C; &nbsp;Your stores and orders are paused</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Your data is safe and preserved</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;You can appeal by contacting support</td></tr>
        </table>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 28px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#ef4444,#f97316);border-radius:10px;box-shadow:0 4px 14px rgba(239,68,68,.3);">
          <a href="mailto:support@dropsigma.com" style="display:inline-block;color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;">Contact Support &rarr;</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 36px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;text-align:center;">
        <p style="margin:0;font-size:13px;color:#64748b;">&#x1F6DF; &nbsp;Think this is a mistake? Reach us at<br/>
        <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:600;text-decoration:none;">support@dropsigma.com</a></p>
      </div>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding:24px 8px 0;">{_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _build_flag_email(name):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#f59e0b,#f97316);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#d97706;background:#fef3c7;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x1F50D; ACCOUNT UNDER REVIEW</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Your account is being reviewed</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, our team has flagged your Drop Sigma account for a routine review. No action is needed from you right now.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td style="padding:24px 48px;">
      <div style="background:#fffbeb;border:1px solid #fde68a;border-left:4px solid #f59e0b;border-radius:10px;padding:18px 20px;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#d97706;text-transform:uppercase;letter-spacing:0.5px;">What to expect</p>
        <p style="margin:0;font-size:14px;color:#78350f;line-height:1.6;">Our team will review your account activity within <strong>2-3 business days</strong>. You will be notified of the outcome via email.</p>
      </div>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 14px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">During the review</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Your account remains active</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Dashboard access is unaffected</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Your data is safe and secure</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x1F4AC; &nbsp;You may be contacted for more info</td></tr>
        </table>
      </div>
    </td></tr>
    <tr><td style="padding:0 48px 36px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;text-align:center;">
        <p style="margin:0;font-size:13px;color:#64748b;">&#x1F6DF; &nbsp;Have questions about this review?<br/>
        <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:600;text-decoration:none;">support@dropsigma.com</a></p>
      </div>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding:24px 8px 0;">{_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _build_delete_email(name):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#6b7280,#374151);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#374151;background:#f3f4f6;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x1F5D1;&#xFE0F; ACCOUNT DELETED</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Your account has been deleted</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, your Drop Sigma account and all associated data has been permanently deleted.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td style="padding:24px 48px;">
      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-left:4px solid #6b7280;border-radius:10px;padding:18px 20px;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:0.5px;">What has been removed</p>
        <p style="margin:0;font-size:14px;color:#4b5563;line-height:1.6;">All your stores, orders, vendor data, team members, and account information have been permanently erased and <strong>cannot be recovered</strong>.</p>
      </div>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 14px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Deleted data includes</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td width="50%" style="padding:4px 0;font-size:13px;color:#6b7280;">&#x1F3EA; &nbsp;All connected stores</td>
            <td width="50%" style="padding:4px 0;font-size:13px;color:#6b7280;">&#x1F4E6; &nbsp;Order history</td>
          </tr>
          <tr>
            <td width="50%" style="padding:4px 0;font-size:13px;color:#6b7280;">&#x1F91D; &nbsp;Vendor profiles</td>
            <td width="50%" style="padding:4px 0;font-size:13px;color:#6b7280;">&#x1F4CA; &nbsp;Stock records</td>
          </tr>
          <tr>
            <td width="50%" style="padding:4px 0;font-size:13px;color:#6b7280;">&#x1F465; &nbsp;Team members</td>
            <td width="50%" style="padding:4px 0;font-size:13px;color:#6b7280;">&#x1F4AC; &nbsp;Email accounts</td>
          </tr>
        </table>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 28px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35);">
          <a href="https://dropsigma.com/signup/" style="display:inline-block;color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;">Create a New Account &rarr;</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 36px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;text-align:center;">
        <p style="margin:0;font-size:13px;color:#64748b;">&#x1F6DF; &nbsp;This was a mistake? Contact us immediately.<br/>
        <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:600;text-decoration:none;">support@dropsigma.com</a></p>
      </div>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding:24px 8px 0;">{_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _build_reactivate_email(name):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#16a34a,#22c55e);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#15803d;background:#dcfce7;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x2705; ACCOUNT REACTIVATED</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Your account is back online!</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, great news — your Drop Sigma account has been fully reactivated. Everything is back to normal.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td style="padding:24px 48px;">
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-left:4px solid #16a34a;border-radius:10px;padding:18px 20px;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#15803d;text-transform:uppercase;letter-spacing:0.5px;">What's restored</p>
        <p style="margin:0;font-size:14px;color:#14532d;line-height:1.6;">Full access to your dashboard, stores, orders, vendors, and all features has been restored. You can continue where you left off.</p>
      </div>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 14px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Everything is back</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Dashboard access restored</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Stores and orders active</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Vendor and team management on</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;All data intact, nothing lost</td></tr>
        </table>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 28px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#16a34a,#22c55e);border-radius:10px;box-shadow:0 4px 14px rgba(22,163,74,.3);">
          <a href="https://dropsigma.com/dashboard/" style="display:inline-block;color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;">Go to Dashboard &rarr;</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 36px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;text-align:center;">
        <p style="margin:0;font-size:13px;color:#64748b;">&#x1F6DF; &nbsp;Need anything? We're here.<br/>
        <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:600;text-decoration:none;">support@dropsigma.com</a></p>
      </div>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding:24px 8px 0;">{_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _build_unflag_email(name):
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/></head>
<body style="margin:0;padding:0;background:#f6f9fc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f6f9fc;padding:48px 16px;">
<tr><td align="center">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td align="center" style="padding-bottom:32px;">{_LOGO}</td></tr>
  <tr><td style="background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;overflow:hidden;">
    <div style="height:4px;background:linear-gradient(90deg,#6366f1,#8b5cf6);"></div>
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center" style="padding:40px 48px 28px;">
      <div style="font-size:12px;font-weight:700;color:#6366f1;background:#ede9fe;border-radius:20px;display:inline-block;padding:5px 16px;letter-spacing:0.5px;margin-bottom:20px;">&#x2714;&#xFE0F; REVIEW COMPLETE</div>
      <h1 style="margin:0 0 12px;font-size:24px;font-weight:800;color:#0f172a;line-height:1.3;">Your account review is complete</h1>
      <p style="margin:0;font-size:15px;color:#64748b;line-height:1.7;">Hi <strong style="color:#0f172a;">{name}</strong>, our team has completed the review of your Drop Sigma account. Everything looks good — no issues were found.</p>
    </td></tr>
    <tr><td style="padding:0 48px;"><div style="height:1px;background:#f1f5f9;"></div></td></tr>
    <tr><td style="padding:24px 48px;">
      <div style="background:#f5f3ff;border:1px solid #ddd6fe;border-left:4px solid #6366f1;border-radius:10px;padding:18px 20px;">
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#6366f1;text-transform:uppercase;letter-spacing:0.5px;">Review outcome</p>
        <p style="margin:0;font-size:14px;color:#3730a3;line-height:1.6;">Your account has been cleared. The review flag has been removed and your account is in good standing.</p>
      </div>
    </td></tr>
    <tr><td style="padding:0 48px 28px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px 24px;">
        <p style="margin:0 0 14px;font-size:12px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;">Account status</p>
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Account cleared — no issues found</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Review flag removed</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;Account in good standing</td></tr>
          <tr><td style="padding:4px 0;font-size:13px;color:#475569;">&#x2705; &nbsp;All features continue to work normally</td></tr>
        </table>
      </div>
    </td></tr>
    <tr><td align="center" style="padding:0 48px 28px;">
      <table cellpadding="0" cellspacing="0"><tr>
        <td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:10px;box-shadow:0 4px 14px rgba(99,102,241,.35);">
          <a href="https://dropsigma.com/dashboard/" style="display:inline-block;color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;">Go to Dashboard &rarr;</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:0 48px 36px;">
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:16px 20px;text-align:center;">
        <p style="margin:0;font-size:13px;color:#64748b;">&#x1F6DF; &nbsp;Have questions? We're always here.<br/>
        <a href="mailto:support@dropsigma.com" style="color:#6366f1;font-weight:600;text-decoration:none;">support@dropsigma.com</a></p>
      </div>
    </td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding:24px 8px 0;">{_FOOTER}</td></tr>
</table>
</td></tr>
</table>
</body></html>"""


def _send_tenant_email(to_email, subject, html):
    def _send():
        try:
            _resend.api_key = os.getenv("RESEND_API_KEY", "")
            _resend.Emails.send({
                "from": "Drop Sigma <noreply@dropsigma.com>",
                "to": [to_email],
                "subject": subject,
                "html": html,
            })
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ── Guards ──────────────────────────────────────────────────────────────────

def superadmin_required(view_fn):
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return redirect(f"/login/?next={request.path}")
        return view_fn(request, *args, **kwargs)
    wrapper.__name__ = view_fn.__name__
    return wrapper


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tenant_dict(t):
    sub = getattr(t, "subscription", None)
    stores_qs  = Store.objects.filter(user=t.user)
    orders_qs  = Order.objects.filter(store__user=t.user)
    vendors_qs = Vendor.objects.filter(assigned_store__user=t.user)
    team_qs    = TeamMember.objects.filter(user__isnull=False)

    return {
        "id":      t.pk,
        "name":    t.name,
        "email":   t.user.email,
        "plan":    t.plan,
        "status":  t.status,
        "stores":  stores_qs.count(),
        "orders":  orders_qs.count(),
        "vendors": vendors_qs.count(),
        "team":    TeamMember.objects.filter(user=t.user).count(),
        "joined":  t.created_at.date().isoformat(),
        "renews":  sub.renews_on.isoformat() if sub and sub.renews_on else None,
        "mrr":     t.mrr,
        "ltv":     t.ltv,
        "notes":   t.notes,
        "flagged":    t.flagged,
        "is_deleted": t.is_deleted,
        "deleted_at": t.deleted_at.date().isoformat() if t.deleted_at else None,
        "payment_status": sub.payment_status if sub else "paid",
    }


def _time_ago(dt):
    diff = timezone.now() - dt
    s = diff.total_seconds()
    if s < 60:       return f"{int(s)}s ago"
    if s < 3600:     return f"{int(s//60)}m ago"
    if s < 86400:    return f"{int(s//3600)}h ago"
    return f"{int(s//86400)}d ago"


# ── Page ─────────────────────────────────────────────────────────────────────

@superadmin_required
def superadmin_page(request):
    return render(request, "superadmin.html")


# ── API: Stats ────────────────────────────────────────────────────────────────

@superadmin_required
@require_GET
def api_stats(request):
    tenants = Tenant.objects.all()
    active  = tenants.filter(status="active")
    trials  = tenants.filter(status="trial")
    mrr     = sum(t.mrr for t in active)
    tenant_user_ids = Tenant.objects.values_list("user_id", flat=True)
    total_orders = Order.objects.filter(store__user_id__in=tenant_user_ids).count()
    return JsonResponse({
        "active_tenants": active.count(),
        "mrr":            mrr,
        "trials":         trials.count(),
        "total_orders":   total_orders,
        "total_tenants":  tenants.count(),
    })


# ── API: Tenants list / create ────────────────────────────────────────────────

@superadmin_required
def api_tenants(request):
    if request.method == "GET":
        tenants = Tenant.objects.select_related("user", "subscription").all()
        return JsonResponse([_tenant_dict(t) for t in tenants], safe=False)

    if request.method == "POST":
        data = json.loads(request.body)
        name  = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        plan  = data.get("plan", "trial")
        notes = data.get("notes", "")

        if not name or not email:
            return JsonResponse({"error": "Name and email required."}, status=400)

        if User.objects.filter(email=email).exists():
            return JsonResponse({"error": "A user with that email already exists."}, status=400)

        username = email.split("@")[0]
        base = username
        n = 1
        while User.objects.filter(username=username).exists():
            username = f"{base}{n}"
            n += 1

        user = User.objects.create_user(
            username=username,
            email=email,
            password=User.objects.make_random_password(),
            is_staff=True,
        )
        user.save()

        trial_ends = datetime.date.today() + datetime.timedelta(days=14)
        status = "trial" if plan == "trial" else "active"

        tenant = Tenant.objects.create(
            user=user,
            name=name,
            plan=plan,
            status=status,
            notes=notes,
            trial_ends=trial_ends,
        )

        price     = PLAN_PRICES.get(plan, 0)
        renews_on = datetime.date.today() + datetime.timedelta(days=30)
        Subscription.objects.create(
            tenant=tenant,
            plan=plan,
            price=price,
            start_date=datetime.date.today(),
            renews_on=renews_on if plan != "trial" else trial_ends,
        )

        TenantActivity.objects.create(
            tenant=tenant,
            action=f"Tenant onboarded by super admin — plan: {plan}",
            action_type="signup",
        )

        return JsonResponse({"ok": True, "tenant": _tenant_dict(tenant)})

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── API: Tenant detail ────────────────────────────────────────────────────────

@superadmin_required
def api_tenant_detail(request, pk):
    t = get_object_or_404(Tenant, pk=pk)

    if request.method == "GET":
        d = _tenant_dict(t)
        d["activities"] = [
            {
                "action":      a.action,
                "action_type": a.action_type,
                "when":        _time_ago(a.created_at),
            }
            for a in t.activities.all()[:20]
        ]
        return JsonResponse(d)

    if request.method == "POST":
        data = json.loads(request.body)
        action = data.get("action")

        if action == "suspend":
            reason = data.get("reason", "")
            t.status = "suspended"
            t.save()
            TenantActivity.objects.create(
                tenant=t,
                action=f"Account suspended — {reason}",
                action_type="general",
            )
            _send_tenant_email(
                t.user.email,
                "Your Drop Sigma account has been suspended",
                _build_suspend_email(t.name, reason),
            )
            return JsonResponse({"ok": True})

        if action == "activate":
            t.status = "active"
            t.save()
            TenantActivity.objects.create(
                tenant=t,
                action="Account reactivated by super admin",
                action_type="general",
            )
            _send_tenant_email(
                t.user.email,
                "Your Drop Sigma account has been reactivated ✓",
                _build_reactivate_email(t.name),
            )
            return JsonResponse({"ok": True})

        if action == "change_plan":
            new_plan = data.get("plan")
            reason   = data.get("reason", "")
            old_plan = t.plan
            t.plan   = new_plan
            if t.status == "trial" and new_plan != "trial":
                t.status = "active"
            t.save()
            sub = getattr(t, "subscription", None)
            if sub:
                sub.plan  = new_plan
                sub.price = PLAN_PRICES.get(new_plan, 0)
                sub.renews_on = datetime.date.today() + datetime.timedelta(days=30)
                sub.save()
            TenantActivity.objects.create(
                tenant=t,
                action=f"Plan changed: {old_plan} → {new_plan}. {reason}".strip(" .") + ".",
                action_type="plan",
            )
            return JsonResponse({"ok": True})

        if action == "add_note":
            note = data.get("note", "").strip()
            if note:
                sep = "\n\n" if t.notes else ""
                t.notes += sep + note
                t.save()
            return JsonResponse({"ok": True, "notes": t.notes})

        if action == "delete":
            confirm = data.get("confirm", "")
            if confirm != "DELETE":
                return JsonResponse({"error": "Type DELETE to confirm."}, status=400)
            t.is_deleted = True
            t.deleted_at = timezone.now()
            t.status     = "deleted"
            t.save()
            TenantActivity.objects.create(
                tenant=t,
                action="Account soft-deleted by super admin",
                action_type="general",
            )
            _send_tenant_email(
                t.user.email,
                "Your Drop Sigma account has been deleted",
                _build_delete_email(t.name),
            )
            return JsonResponse({"ok": True})

        if action == "restore":
            t.is_deleted = False
            t.deleted_at = None
            t.status     = "trial"
            t.save()
            TenantActivity.objects.create(
                tenant=t,
                action="Account restored (delete revoked) by super admin",
                action_type="general",
            )
            return JsonResponse({"ok": True})

        if action == "flag":
            t.flagged = not t.flagged
            t.save()
            if t.flagged:
                _send_tenant_email(
                    t.user.email,
                    "Your Drop Sigma account is under review",
                    _build_flag_email(t.name),
                )
            else:
                _send_tenant_email(
                    t.user.email,
                    "Your Drop Sigma account review is complete ✓",
                    _build_unflag_email(t.name),
                )
            return JsonResponse({"ok": True, "flagged": t.flagged})

        return JsonResponse({"error": "Unknown action."}, status=400)

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── API: Activity log ─────────────────────────────────────────────────────────

@superadmin_required
@require_GET
def api_activity(request):
    acts = TenantActivity.objects.select_related("tenant").all()[:60]
    return JsonResponse([
        {
            "tenant":      a.tenant.name,
            "action":      a.action,
            "action_type": a.action_type,
            "when":        _time_ago(a.created_at),
        }
        for a in acts
    ], safe=False)


# ── Impersonation ─────────────────────────────────────────────────────────────

@superadmin_required
def impersonate(request, pk):
    tenant = get_object_or_404(Tenant, pk=pk)
    TenantActivity.objects.create(
        tenant=tenant,
        action="Super admin started impersonation session",
        action_type="general",
    )
    request.session["impersonate_id"]   = tenant.user.pk
    request.session["impersonate_name"] = tenant.name
    request.session["impersonate_email"] = tenant.user.email
    return redirect("/dashboard/")


@superadmin_required
def exit_impersonation(request):
    request.session.pop("impersonate_id",    None)
    request.session.pop("impersonate_name",  None)
    request.session.pop("impersonate_email", None)
    return redirect("/superadmin/")


# ── API: Coupons ──────────────────────────────────────────────────────────────

def _coupon_dict(c):
    return {
        "id":             c.pk,
        "code":           c.code,
        "discount_type":  c.discount_type,
        "discount_value": float(c.discount_value),
        "max_uses":       c.max_uses,
        "uses":           c.uses,
        "is_active":      c.is_active,
        "expires_at":     c.expires_at.isoformat() if c.expires_at else None,
        "created_at":     c.created_at.date().isoformat(),
    }


@superadmin_required
def api_coupons(request):
    if request.method == "GET":
        coupons = Coupon.objects.all().order_by("-created_at")
        return JsonResponse([_coupon_dict(c) for c in coupons], safe=False)

    if request.method == "POST":
        data = json.loads(request.body)
        code           = data.get("code", "").strip().upper()
        discount_type  = data.get("discount_type", "flat")
        discount_value = data.get("discount_value")
        max_uses       = data.get("max_uses") or None
        expires_at     = data.get("expires_at") or None

        if not code:
            return JsonResponse({"error": "Code is required."}, status=400)
        if not discount_value:
            return JsonResponse({"error": "Discount value is required."}, status=400)
        if Coupon.objects.filter(code=code).exists():
            return JsonResponse({"error": "Coupon code already exists."}, status=400)

        c = Coupon.objects.create(
            code=code,
            discount_type=discount_type,
            discount_value=discount_value,
            max_uses=max_uses,
            expires_at=expires_at,
        )
        return JsonResponse({"ok": True, "coupon": _coupon_dict(c)})

    return JsonResponse({"error": "Method not allowed."}, status=405)


@superadmin_required
def api_coupon_detail(request, pk):
    c = get_object_or_404(Coupon, pk=pk)

    if request.method == "POST":
        data   = json.loads(request.body)
        action = data.get("action")

        if action == "toggle":
            c.is_active = not c.is_active
            c.save()
            return JsonResponse({"ok": True, "is_active": c.is_active})

        if action == "delete":
            c.delete()
            return JsonResponse({"ok": True})

        return JsonResponse({"error": "Unknown action."}, status=400)

    return JsonResponse({"error": "Method not allowed."}, status=405)


# ── Public: Validate coupon (called from upgrade/checkout page) ───────────────

def api_validate_coupon(request):
    code     = request.GET.get("code", "").strip().upper()
    plan_key = request.GET.get("plan", "pro")

    PLAN_PRICES_DISPLAY = {"pro": 99, "starter": 49, "growth": 99, "scale": 149}
    original = PLAN_PRICES_DISPLAY.get(plan_key, 99)

    try:
        c = Coupon.objects.get(code=code)
    except Coupon.DoesNotExist:
        return JsonResponse({"valid": False, "error": "Invalid coupon code."})

    valid, msg = c.is_valid()
    if not valid:
        return JsonResponse({"valid": False, "error": msg})

    discounted = c.apply(original)
    if c.discount_type == "flat":
        label = f"${float(c.discount_value):.0f} off"
    else:
        label = f"{float(c.discount_value):.0f}% off"

    return JsonResponse({
        "valid":      True,
        "label":      label,
        "original":   original,
        "discounted": round(discounted, 2),
        "savings":    round(original - discounted, 2),
    })
