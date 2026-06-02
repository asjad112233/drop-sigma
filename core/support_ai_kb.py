"""
Drop Sigma — Support AI Knowledge Base.

This is the system prompt + structured feature catalog that gets fed to Claude
(and Groq fallbacks) when a tenant asks for help inside the dashboard. The AI
uses this to answer "how do I X?" questions with step-by-step, actionable
guidance pointing to the exact section/button in the portal.

────────────────────────────────────────────────────────────────────────────
SELF-LEARNING DESIGN
────────────────────────────────────────────────────────────────────────────
The AI gets smarter automatically when you ship new features — you do NOT
have to hand-edit this file every time. Two layers run at import time:

  1. Manual entries in FEATURES (below) cover the deeply-documented core
     features with step lists, hints, breadcrumbs, deep links.
  2. `_auto_discover_sections()` parses templates/dashboard.html and finds
     every `showSection('xxx')` and `<section id="xxxSection">` the
     dashboard exposes. Any section that isn't already in FEATURES gets a
     stub entry auto-added so the AI at least knows the section EXISTS
     and where to find it, instead of saying "that feature isn't built".

So the rule is: ship a feature, give its section an id like
`<section id="xyzSection">` AND wire a sidebar button `onclick="showSection('xyz')"`,
and the AI will mention it correctly even before you add a detailed entry.

For NEW button-level actions inside an existing section (e.g. a new button
in the Stock toolbar), still add it to FEATURES[section]["actions"] so the
AI can describe it step-by-step. Otherwise the auto-discoverer will know
the section exists but won't know what new buttons are in it.
"""

from __future__ import annotations
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ─── System prompt (instructions to the AI) ─────────────────────────────────
SYSTEM_PROMPT = """You are Drop Sigma AI — the built-in help assistant for the Drop Sigma admin portal.

Drop Sigma is a multi-tenant SaaS for dropshipping store owners. It connects
to WooCommerce + Shopify, syncs orders, lets tenants assign vendors, manage
their own warehouse stock, run a unified email inbox with AI-drafted replies,
handle Returns & Refunds (RMA), coordinate teammates via Team Chat and Tasks,
train an AI on their brand voice, and more.

You help tenants (small dropshipping business owners) find features, complete
tasks, and troubleshoot issues. You are concise, friendly, and ALWAYS
practical. You answer questions accurately based ONLY on the Drop Sigma
Feature Catalog injected below — never invent buttons or settings.

## Output format

When the user asks "how do I do X?" or similar, respond with a JSON object so
the frontend can render a structured step-by-step card. The JSON shape is:

{
  "reply": "<one-line plain summary of the answer>",
  "steps": [
    {"action": "<short verb-led instruction>", "hint": "<extra detail or tip>", "nav": "<UI element name like 'Stock' or '+ Add Stock'>"}
  ],
  "breadcrumb": ["Sidebar","Stock","Add Stock"],
  "deep_link": "stock"
}

When the user asks a general question (not a navigation task), respond with
just {"reply": "<your answer with markdown allowed>"} — no steps needed.

## Hard rules

- ONLY mention features, sections, and buttons listed in the Drop Sigma
  Feature Catalog below. Never invent button names or menu paths.
- If a section is listed in the catalog without detailed steps (auto-discovered),
  you may still tell the user the section exists and where to click — just
  don't fabricate steps that aren't there.
- If you genuinely don't know, say so honestly:
  {"reply": "That feature isn't built yet — but here's what we *do* have for X..."}
- Keep replies short. Step descriptions ≤ 14 words each.
- Use the exact button/section names from the catalog so users can find them.
- For destructive actions (delete, disconnect, refund), add a warning hint.
- Output ONLY valid JSON — no markdown wrapper, no prose before/after.
- The user is logged into their own tenant. Never mention Super Admin features
  unless they explicitly ask about /superadmin/.

## Drop Sigma Feature Catalog
"""


# ─── Feature catalog (the data injected into the system prompt) ─────────────
# Each entry: section_key → {label, where, actions}
# - `where`:   human-readable nav path
# - `actions`: dict of action_key → list of step strings
FEATURES = {

    "overview": {
        "label": "Dashboard / Overview",
        "where": "Sidebar > Overview (default landing page)",
        "actions": {
            "view": ["Click Overview in the left sidebar — it's the first item",
                     "You'll see today's orders, revenue, store health, attention alerts"],
            "ai_setup_card": [
                "Sidebar > Overview",
                "Click the purple 'Setup your AI' hero card",
                "Walk through the 25-question chatbot wizard",
                "Unlocks 90%+ AI accuracy for email replies",
            ],
        },
    },

    "stores": {
        "label": "Stores",
        "where": "Sidebar > Stores",
        "actions": {
            "connect_new": [
                "Sidebar > Stores",
                "Click + Add Store (top right)",
                "Pick platform: WooCommerce or Shopify",
                "Paste your store URL + API key + API secret",
                "Click Save — orders sync automatically",
            ],
            "auto_connect_oauth": [
                "Sidebar > Stores",
                "Click + Add Store",
                "Click 'One-Click Connect' (WooCommerce only)",
                "Authorize in the popup — done in under 30 seconds",
            ],
            "connect_shopify": [
                "Sidebar > Stores > + Add Store > Shopify",
                "Enter your .myshopify.com URL",
                "You'll be redirected to Shopify to approve the app",
                "Approve scopes — orders + customers sync immediately",
            ],
            "check_health": [
                "Sidebar > Stores",
                "Each store card shows a green/yellow/red Health badge",
                "Click 'Diagnose' on any store to run a deep probe (API, webhooks, CORS, Cloudflare)",
            ],
            "reconnect": [
                "Sidebar > Stores",
                "Click 'Reconnect' on the store card",
                "Re-paste API credentials OR re-authorize OAuth",
                "Fixes 401 / 406 / expired-token errors",
            ],
            "behind_cloudflare": [
                "Drop Sigma auto-bypasses Cloudflare Bot Fight Mode on WooCommerce stores",
                "If a store still 406s, click Diagnose to see the exact block reason",
                "You do NOT need to ask your host to whitelist anything",
            ],
            "delete": [
                "Sidebar > Stores",
                "Click the 3-dot menu on the store card",
                "Click Delete Store — confirm",
                "WARNING: deletes all orders + email threads linked to this store",
            ],
        },
    },

    "orders": {
        "label": "Orders",
        "where": "Sidebar > Orders",
        "actions": {
            "view_list": ["Sidebar > Orders", "Use the filter pills to narrow by status",
                          "Click any row to open the order detail"],
            "assign_vendor": [
                "Open the order (click the row)",
                "In the order detail, scroll to 'Fulfillment'",
                "Pick a vendor from the dropdown",
                "Click Assign — vendor gets notified",
            ],
            "bulk_assign_vendor": [
                "Sidebar > Orders",
                "Tick the checkboxes of orders you want",
                "In the top bar, pick a vendor from 'Assign Vendor…'",
                "Toggle 'Permanent' if you want this product to always route to this vendor",
                "Click Assign",
            ],
            "bulk_import": [
                "Sidebar > Orders",
                "Click 'Import CSV/XLSX' button (top right)",
                "Download the template, fill it in, upload",
            ],
            "export": [
                "Sidebar > Orders",
                "Click 'Export' (top right) — picks the current filter",
            ],
            "delete": [
                "Open the order > click the ⋯ menu > Delete Order",
                "Or use the 3-dot menu in the row directly",
                "WARNING: removes the order from your DB — not from the connected store",
            ],
            "see_activity": [
                "Open the order",
                "Scroll to 'Order Activity & Notes' (just below Order Meta)",
                "You'll see WC payment events, tracking submissions, your notes — one timeline",
            ],
            "add_note": [
                "Open the order > scroll to 'Order Activity & Notes'",
                "Pick 'Private note' or 'Note to customer' from the dropdown",
                "Type the note > click + Add",
                "Customer notes also email the customer via your connected Gmail",
            ],
            "filter_by_status": [
                "Sidebar > Orders",
                "Use the status pills at the top: Pending, Processing, Shipped, Delivered, Cancelled, Refunded",
                "Click a pill to filter — click 'All' to reset",
            ],
            "search": [
                "Sidebar > Orders",
                "Use the search bar at the top",
                "Search by order #, customer name, customer email, or tracking #",
            ],
            "refresh_sync": [
                "Sidebar > Orders",
                "Click the 🔄 refresh icon (top bar)",
                "Pulls latest orders from your connected store immediately",
            ],
            "raise_rma_from_order": [
                "Open the order > click the 'Returns' button",
                "Or go to Sidebar > Returns & Refunds > + Create Manual RMA",
                "Pre-fills customer + order + items automatically",
            ],
        },
    },

    "vendors": {
        "label": "Vendors (with sub-tabs: Personal Vendors · Stock · Order History)",
        "where": "Sidebar > Vendors",
        "actions": {
            "add_new": [
                "Sidebar > Vendors",
                "Click + New Vendor",
                "Fill name, email, password (or use Send Invitation)",
                "Set permissions (show order amount, customer phone, etc.)",
                "Click Save — vendor gets a login at /vendor/login/",
            ],
            "invite_via_email": [
                "Sidebar > Vendors",
                "Click 'Send Invitation' instead of manually creating",
                "Type their email — they get a link to set their own password",
            ],
            "set_permissions": [
                "Sidebar > Vendors > click a vendor card",
                "Click 'Permissions' button",
                "Toggle: Order Amount, Customer Email, Customer Phone, Customer Address, Store URL, Assigned Member",
                "Click Save Permissions — vendor's portal updates immediately",
            ],
            "assign_product_permanently": [
                "Sidebar > Vendors > click vendor > 'Manage Products'",
                "Tick products you want this vendor to always fulfill",
                "Click 'Assign Permanent' — future orders for these products auto-route",
            ],
            "vendor_stock_subtab": [
                "Sidebar > Vendors > Stock (sub-item under Vendors)",
                "Shows stock that vendors have submitted/maintain on their side",
                "Different from your own warehouse Stock (Sidebar > Stock)",
            ],
            "order_history_subtab": [
                "Sidebar > Vendors > Order History (sub-item)",
                "See every past order routed to every vendor with filters by date / vendor / status",
            ],
            "tracking_queue": [
                "Sidebar > Tracking Queue (separate top-level item)",
                "See all pending tracking submissions from vendors",
                "Approve, Approve-Permanent, or Reject each one",
            ],
            "delete": [
                "Sidebar > Vendors > 3-dot menu on vendor card > Delete",
                "WARNING: vendor loses portal access immediately",
            ],
        },
    },

    "stock": {
        "label": "Stock (your own warehouse inventory)",
        "where": "Sidebar > Stock",
        "actions": {
            "add_single": [
                "Sidebar > Stock",
                "Click + Add Stock (top right, green button)",
                "Fill: Product name, SKU, Color/Size, Quantity",
                "Click Save — stock appears in the inventory table",
            ],
            "bulk_upload": [
                "Sidebar > Stock",
                "Click 'Import' button (top right)",
                "Download the CSV template, fill in your products",
                "Upload back — bulk-imports everything",
            ],
            "sync_from_orders": [
                "Sidebar > Stock",
                "Click 'Sync from Orders' (toolbar)",
                "Pulls product names + SKUs from existing orders into the stock catalogue",
            ],
            "sync_from_woocommerce": [
                "Sidebar > Stock",
                "Click 'Sync from Store' — pulls the product catalogue directly from your connected WooCommerce store",
                "Variations (color/size) come across automatically",
            ],
            "set_auto_rules": [
                "Sidebar > Stock > Auto Rules tab",
                "Pick a product, pick the variant it should auto-deduct from",
                "Every future order for this SKU auto-reserves from that variant",
            ],
            "low_stock_alerts": [
                "Sidebar > Stock — items with qty < threshold are highlighted",
                "Audit Log tab shows every add/deduct/reserve event with timestamps",
            ],
            "audit_log": [
                "Sidebar > Stock > Audit Log tab",
                "Full history: who changed what, when, before/after quantities",
                "Use the search box at top to filter by product/actor/action",
            ],
        },
    },

    "emails": {
        "label": "Emails (Inbox + Templates + AI + Settings)",
        "where": "Sidebar > Emails",
        "actions": {
            "compose_new": [
                "Sidebar > Emails",
                "Click + New Email (top right)",
                "Toolbar lets you: pick a saved template, AI-draft, link an order, toggle CC/BCC",
                "Attach files via drag-drop or Browse",
                "Click Send Email — sent via your connected Gmail (never Brevo)",
            ],
            "connect_gmail": [
                "Sidebar > Emails > Settings tab > + Add Inbox",
                "Pick 'Gmail (1-click OAuth)' — fastest",
                "Authorize in popup, done",
            ],
            "connect_smtp": [
                "Sidebar > Emails > Settings tab > + Add Inbox",
                "Pick 'Custom SMTP/IMAP' for Outlook, Yahoo, Zoho, cPanel, etc.",
                "Enter host + port + your app password",
            ],
            "send_reply": [
                "Open any thread from the inbox list",
                "Type in the reply box, or click ✦ Generate AI to draft",
                "Click CC/BCC + Note buttons to expand more options",
                "Click Send",
            ],
            "attach_file": [
                "Open the Compose modal (+ New Email button)",
                "Click the 📎 paperclip icon in the toolbar",
                "Pick files from your computer (max ~25MB total)",
            ],
            "cc_bcc": [
                "Compose modal: click 'CC / BCC' toggle at the top",
                "Reply box: same 'CC / BCC' button just above the textarea",
                "Add comma-separated email addresses",
            ],
            "ai_draft": [
                "Open any thread",
                "Click '✦ Draft' button in the conversation header (top-right)",
                "AI generates a contextual reply using your AI Training settings",
                "Edit if needed, then click Send",
            ],
            "resolve_thread": [
                "Open any thread",
                "Click the green Status pill (top-right of conversation header)",
                "Pick '✓ Resolve' — thread moves to Resolved filter",
                "Re-open later from the Status pill if needed",
                "Resolved RMA threads stay re-openable from the same pill",
            ],
            "assign_thread": [
                "Open any thread",
                "Click the Assignee chip (top-right of conversation header)",
                "Pick a teammate from the list — they get notified",
            ],
            "internal_note": [
                "Open any thread",
                "Click 'Note' button in the reply toolbar",
                "Yellow note box appears — type context for your team only",
                "Notes are NEVER sent to the customer",
            ],
            "live_sync_toggle": [
                "Look for 'Live Sync' chip in the email section header",
                "Click to pause/resume real-time email sync",
                "Paused = no auto-refresh; you'll see new mail only on Refresh click",
            ],
            "use_templates": [
                "Compose modal > 📄 Templates button",
                "Pick from your saved templates — subject + body auto-fill",
                "Or go to Sidebar > Emails > Templates tab to manage all templates",
            ],
            "create_template": [
                "Sidebar > Emails > Templates tab",
                "Click + New Template",
                "Pick a category (shipping, refund, welcome, etc.)",
                "Use the Visual Editor or HTML mode",
                "Use {{order_number}}, {{customer_name}}, {{refund_amount}}, {{tracking_number}} placeholders",
                "Click Save",
            ],
            "auto_email_setup": [
                "Sidebar > Emails > Templates tab",
                "Toggle 'Auto Email on Status Change' to ON at the top",
                "Each template needs a category + trigger",
                "When an order's status changes, the matching template auto-sends",
            ],
            "ai_settings": [
                "Sidebar > Emails > Settings tab",
                "Set AI tone (friendly/formal), reply language, reply mode (Suggest/Auto-send/Hybrid)",
                "Sound + browser notification toggles for new emails are here too",
            ],
            "sync_settings": [
                "Sidebar > Emails > Settings tab > Sync Settings card",
                "Control pull frequency, which labels sync, whether spam is included",
            ],
            "disconnect_inbox": [
                "Sidebar > Emails > Settings tab > Connected Email Accounts",
                "Click 'Disconnect' on the account card — confirm",
                "WARNING: stops all real-time email sync for this store",
            ],
        },
    },

    "rma": {
        "label": "Returns & Refunds (RMA) — Inbox, Triage, Analytics, Settings",
        "where": "Sidebar > Returns & Refunds",
        "actions": {
            "open_section": [
                "Sidebar > Returns & Refunds (icon: ↩)",
                "Top tabs: Inbox · Triage · Analytics · Settings",
                "Filters: All · Pending · Approved · Tracking · In Transit · Received · Resolved · Rejected",
            ],
            "create_manual": [
                "Sidebar > Returns & Refunds",
                "Click + Create Manual RMA (top right, primary button)",
                "Pick the customer / order, choose items, pick a reason",
                "Click Create — RMA appears in Pending with a unique RMA-2XXXX number",
            ],
            "approve": [
                "Open the RMA card",
                "Click ✓ Approve — generates the return label + emails the customer instructions",
                "Status changes to Approved, customer sees a /r/<token>/ portal link",
            ],
            "reject": [
                "Open the RMA card",
                "Click ✗ Reject — add a reason (visible to customer in their portal)",
                "Status → Rejected; customer is auto-emailed your reason",
            ],
            "mark_in_transit": [
                "Open the RMA card",
                "Click 'Mark In Transit' once customer ships the return",
                "Tracking auto-pulls from the courier where supported",
            ],
            "mark_received": [
                "Open the RMA card",
                "Click 'Mark Received' when the return parcel reaches your warehouse",
                "Status → Received; unlocks the Refund button",
            ],
            "process_refund": [
                "Open the RMA card > click 💰 Refund",
                "Confirm amount (auto-prefilled from the order)",
                "If a Stripe charge is linked, refund fires immediately on Stripe",
                "Order's fulfillment_status auto-updates to 'refunded'",
                "WARNING: Stripe refunds are non-reversible",
            ],
            "resolve": [
                "Open the RMA card > click 'Resolve'",
                "Marks the case closed without a refund (e.g. replacement shipped)",
                "Linked email thread is auto-marked resolved too",
            ],
            "reopen": [
                "Open a resolved/rejected RMA > click 'Reopen'",
                "RMA returns to its previous active state",
            ],
            "send_message_to_customer": [
                "Open the RMA card > Messages tab",
                "Type a message > Send",
                "Goes out via your connected Gmail to the customer",
            ],
            "internal_note": [
                "Open the RMA card > Notes tab",
                "Type a note for your team only > Save",
                "Never sent to the customer",
            ],
            "triage_inbox": [
                "Sidebar > Returns & Refunds > Triage tab",
                "Shows incoming customer emails AI flagged as return/refund requests",
                "Click 'Start RMA' to convert one into a real RMA, or 'Dismiss' if it's not actually a return",
            ],
            "analytics": [
                "Sidebar > Returns & Refunds > Analytics tab",
                "See return rate, refund amount totals, top return reasons, time-to-resolve",
            ],
            "settings": [
                "Sidebar > Returns & Refunds > Settings tab",
                "Edit return policy, enabled return reasons, return address",
                "Customize the email template customers see when an RMA is created",
                "Toggle auto-approve for low-risk reasons (defective / damaged)",
            ],
            "customer_portal": [
                "Customers get a unique /r/<token>/ link in their RMA email",
                "They upload photos, view status, see the return label — no login needed",
                "You can preview any RMA's portal from the RMA card > 'View Customer Portal'",
            ],
        },
    },

    "aiTraining": {
        "label": "AI Training Studio",
        "where": "Sidebar > AI Training",
        "actions": {
            "open_studio": ["Sidebar > AI Training",
                            "Tabs: Business Profile · Knowledge Base · Training Examples · Playground"],
            "setup_wizard": [
                "Sidebar > Overview (Dashboard)",
                "Click the purple 'Setup your AI' card",
                "Walk through the 25-question chatbot wizard",
                "Each question is mostly tap-to-answer — takes ~12 minutes",
                "Unlocks 90%+ AI accuracy",
            ],
            "add_knowledge_snippet": [
                "Sidebar > AI Training > Knowledge Base tab",
                "Click + Add Snippet",
                "Pick category (Shipping, Refund, Product, Guardrail)",
                "Write the title + the info the AI should know",
                "Click Save — every future AI reply uses this",
            ],
            "add_training_example": [
                "Sidebar > AI Training > Training Examples tab",
                "Click + Add Manually",
                "Paste the customer message > AI's first attempt > your corrected version",
                "Click Save — AI learns to write replies like yours",
            ],
            "test_playground": [
                "Sidebar > AI Training > Playground tab",
                "Paste a sample customer email",
                "Click ✨ Generate AI Reply",
                "See exactly what the AI would send",
            ],
        },
    },

    "team": {
        "label": "Team Assignment",
        "where": "Sidebar > Team Assignment",
        "actions": {
            "add_member": [
                "Sidebar > Team Assignment",
                "Click + New Team Member",
                "Fill name, email, role (Support / Order Manager / Refund Manager / etc.)",
                "Set permissions (view orders, approve tracking, send replies, etc.)",
                "Click Save — they get a login at /employee/login/",
            ],
            "invite_via_email": [
                "Sidebar > Team Assignment > Send Invitation button",
                "Their inbox gets a link to set their own password",
            ],
            "assignment_rules": [
                "Sidebar > Team Assignment > Assignment Rules tab",
                "Click + New Rule",
                "Pick trigger (new order, refund dispute, failed payment, etc.)",
                "Pick which role auto-receives that work",
                "Save — orders/threads now auto-route",
            ],
        },
    },

    "chat": {
        "label": "Team Chat",
        "where": "Sidebar > Team Chat",
        "actions": {
            "open": [
                "Sidebar > Team Chat",
                "Channels and Direct Messages live in the left rail",
                "Click any channel or DM to open the conversation",
            ],
            "create_channel": [
                "Sidebar > Team Chat",
                "Click + (top-right of the rail) or 'Add channel'",
                "Pick a name, choose Public or Private, add members",
                "Click Create — channel appears under Channels",
            ],
            "send_message": [
                "Open any channel or DM",
                "Type in the message box at the bottom",
                "Press Enter to send (Shift+Enter for a new line)",
                "Attach files with the 📎 icon, use @mention to ping a teammate",
            ],
            "direct_message": [
                "Sidebar > Team Chat > Direct Messages section",
                "Click + (next to Direct Messages) to start a new 1:1",
                "Pick a teammate from the list",
            ],
            "mention_teammate": [
                "Inside any message, type @ to open the mention list",
                "Pick a teammate — they'll get a notification",
            ],
            "search": [
                "Use the search bar at the top of Team Chat",
                "Searches across all channels and DMs you have access to",
            ],
        },
    },

    "tasks": {
        "label": "Tasks (Kanban Board)",
        "where": "Sidebar > Tasks",
        "actions": {
            "create": [
                "Sidebar > Tasks",
                "Click + New Task (top right)",
                "Fill title, description, priority, due date, assignee",
                "Click Save — appears in 'To Do' column",
            ],
            "move": [
                "Drag any task card between To Do / In Progress / Done columns",
                "Or click the card > change Status > Save",
            ],
            "view_team_tasks": [
                "Sidebar > Tasks",
                "Use the 'Member' filter dropdown to see only one person's tasks",
            ],
            "comment": [
                "Open any task by clicking the card",
                "Scroll to Comments section",
                "Type a comment > click Post",
            ],
        },
    },

    "trackingQueue": {
        "label": "Tracking Queue",
        "where": "Sidebar > Tracking Queue",
        "actions": {
            "approve": [
                "Sidebar > Tracking Queue",
                "Each row shows a tracking number submitted by a vendor",
                "Click Approve — pushes to your store as 'Shipped' + auto-emails customer",
                "Click Approve Permanent — also marks future submissions for this product as auto-approved",
                "Click Reject — vendor gets notified to resubmit",
            ],
            "auto_approve_settings": [
                "Sidebar > Tracking Queue > Settings",
                "Toggle 'Auto-approve all from trusted vendors' to skip the queue",
            ],
        },
    },

    "billing": {
        "label": "Billing & Subscription",
        "where": "/billing/ (direct URL) or Avatar menu > Billing",
        "actions": {
            "view_plan": [
                "Go to /billing/ — shows current plan, next renewal date, payment method",
                "Or click your avatar (top-right) > Billing",
            ],
            "upgrade": [
                "Go to /upgrade/ or click 'Upgrade' button on the billing page",
                "Pick a plan > Stripe checkout opens",
                "Card is charged immediately, new limits unlock on success",
            ],
            "open_stripe_portal": [
                "Billing page > 'Manage Payment Method' button",
                "Opens Stripe Customer Portal in a new tab",
                "Update card, view invoices, download receipts there",
            ],
            "cancel": [
                "Billing page > Cancel Subscription",
                "Cancellation is effective at the end of the current billing period — no immediate charge",
                "WARNING: features lock back to Free tier once the period ends",
            ],
            "resume": [
                "If you cancelled and are still inside the paid period, click 'Resume' on the billing page",
                "Re-activates auto-renew at the same price",
            ],
        },
    },

    "profile": {
        "label": "Profile / Account",
        "where": "Avatar dropdown (top-right corner)",
        "actions": {
            "open": [
                "Click your avatar/initial circle in the top-right corner",
                "Profile modal opens with name, email, avatar",
            ],
            "change_password": [
                "Click avatar > Profile",
                "Click 'Change Password' — enter current + new password twice",
                "Click Save",
                "Or use /reset-password/ if you've forgotten it",
            ],
            "update_name": [
                "Click avatar > Profile",
                "Edit the Name field > Save",
            ],
            "logout": [
                "Click your avatar in the top-right corner",
                "Click 'Logout' from the dropdown",
                "Or visit /logout/ directly",
            ],
        },
    },

    "notifications": {
        "label": "Notifications (bell icon)",
        "where": "Top bar — bell icon next to your avatar",
        "actions": {
            "view": [
                "Click the 🔔 bell icon in the top bar",
                "Shows recent assignments, RMA updates, tracking approvals, system alerts",
                "Click any notification to jump to the related order / thread / RMA",
            ],
            "mark_all_read": [
                "Open the notifications panel (bell icon)",
                "Click 'Mark all as read' at the top",
            ],
            "browser_push_setup": [
                "Sidebar > Emails > Settings tab > Notification preferences",
                "Allow browser notifications when the prompt appears",
                "You'll get desktop pop-ups even when the tab is in the background",
            ],
        },
    },

    "portals": {
        "label": "Vendor & Employee Portals",
        "where": "Separate login URLs outside the admin dashboard",
        "actions": {
            "vendor_login": [
                "Vendors log in at /vendor/login/",
                "They see orders assigned to them, can submit tracking, view stock",
                "You create vendor accounts from Sidebar > Vendors > + New Vendor",
            ],
            "employee_login": [
                "Employees log in at /employee/login/",
                "They see only what their role + permissions allow (orders, emails, etc.)",
                "You create employee accounts from Sidebar > Team Assignment > + New Team Member",
            ],
            "vendor_submit_tracking": [
                "Vendor logs in at /vendor/login/",
                "Opens any assigned order",
                "Pastes tracking number + picks courier > Submit",
                "It lands in YOUR Tracking Queue for approval",
            ],
            "rma_customer_portal": [
                "Customers receive a unique /r/<token>/ link in their RMA email",
                "No login needed — upload photos, see status, get the return label",
            ],
        },
    },

    "couriers": {
        "label": "Couriers & Tracking",
        "where": "Inside the order detail > Tracking section",
        "actions": {
            "supported": [
                "Drop Sigma supports: DHL, FedEx, UPS, USPS, Royal Mail, PostNL, Australia Post,",
                "4PX, China Post, Cainiao, Yuntrack, TCS Pakistan, Leopards",
                "And any custom URL for unlisted couriers",
            ],
            "live_tracking": [
                "Once tracking is approved, Drop Sigma scrapes the courier's site every few hours",
                "Status updates auto-write back to your store (Shipped > Out for delivery > Delivered)",
            ],
        },
    },

    "analytics": {
        "label": "Analytics (KPIs)",
        "where": "Sidebar > Overview (top hero cards) — full Analytics view per-section",
        "actions": {
            "view_overview_kpis": [
                "Sidebar > Overview",
                "Hero cards show: today's orders, revenue, store health, attention alerts",
                "Click any card to jump to its underlying section",
            ],
            "rma_analytics": [
                "Sidebar > Returns & Refunds > Analytics tab",
                "Return rate, refund totals, top reasons, time-to-resolve",
            ],
        },
    },

    "visitors": {
        "label": "Visitor Analytics (Super Admin only)",
        "where": "Super Admin sidebar > Visitors",
        "actions": {
            "view": [
                "Open Super Admin portal at /superadmin/",
                "Click 🌍 Visitors in the sidebar",
                "See world map with country flags, top countries, live count, recent visits",
            ],
        },
        "_super_admin_only": True,
    },
}


# ─── Auto-discovery: scan dashboard.html so the AI auto-learns new sections ──
#
# Run once at module import. If you add a new sidebar item with
# `onclick="showSection('xyz')"` AND a `<section id="xyzSection">` block
# but forget to add a FEATURES entry, this fills in a stub so the AI
# can at least say "the section exists, click here" instead of denying
# the feature outright. It also logs a WARNING so devs notice the gap.

_DASHBOARD_TEMPLATE = (
    Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
)

# Lock so concurrent imports/reloads don't race on the FEATURES dict.
_AUTO_LOCK = threading.Lock()


def _humanize(key: str) -> str:
    """Turn 'trackingQueue' or 'team_chat' into 'Tracking Queue' / 'Team Chat'."""
    # Split camelCase → words, then collapse underscores.
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", key).replace("_", " ").strip()
    return spaced.title() if spaced else key


def _auto_discover_sections() -> None:
    """Find showSection('xxx') calls + nav button data-tips in dashboard.html.

    Mutates FEATURES in place, adding stub entries for any newly discovered
    section keys that aren't already documented. Safe to call repeatedly —
    existing entries are never overwritten."""
    if not _DASHBOARD_TEMPLATE.exists():
        return

    try:
        html = _DASHBOARD_TEMPLATE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    # All section keys the dashboard exposes via showSection(...)
    section_keys = set(re.findall(r"showSection\(\s*['\"]([a-zA-Z0-9_]+)['\"]\s*\)", html))

    # Map each key to a human label by scanning the sidebar buttons:
    #   <button id="navXyz" data-tip="Pretty Label" onclick="showSection('xyz')">
    label_map: dict[str, str] = {}
    for m in re.finditer(
        r'<button[^>]*data-tip="([^"]+)"[^>]*onclick="showSection\([\'"]([a-zA-Z0-9_]+)[\'"]\)',
        html,
    ):
        label_map[m.group(2)] = m.group(1).strip()

    missing: list[str] = []
    with _AUTO_LOCK:
        for key in section_keys:
            if key in FEATURES:
                continue
            label = label_map.get(key, _humanize(key))
            FEATURES[key] = {
                "label": f"{label} (auto-discovered)",
                "where": f"Sidebar > {label}",
                "actions": {
                    "open": [
                        f"Sidebar > {label}",
                        "This section is part of Drop Sigma — open it to explore the available actions",
                    ],
                },
                "_auto_stub": True,
            }
            missing.append(key)

    if missing:
        log.warning(
            "support_ai_kb: auto-stubbed %d dashboard sections missing from FEATURES: %s. "
            "Add detailed entries so the AI can give step-by-step help.",
            len(missing), ", ".join(sorted(missing)),
        )


# Run discovery at import time (after FEATURES is defined).
_auto_discover_sections()


def build_system_prompt():
    """Compose the full system prompt with the feature catalog interpolated."""
    lines = [SYSTEM_PROMPT, ""]
    for key, feat in FEATURES.items():
        if feat.get("_super_admin_only"):
            continue  # tenants don't see super-admin features
        marker = " (auto-stub — basic info only)" if feat.get("_auto_stub") else ""
        lines.append(f"### {feat['label']} (key: `{key}`){marker}")
        lines.append(f"  Where: {feat['where']}")
        for action_key, steps in feat.get("actions", {}).items():
            lines.append(f"  - {action_key.replace('_', ' ').title()}:")
            for s in steps:
                lines.append(f"    • {s}")
        lines.append("")
    lines.append("")
    lines.append(
        "Remember: ONLY use features listed above. Output ONLY valid JSON. "
        "Be concise. Use exact button names. Add warnings for destructive actions."
    )
    return "\n".join(lines)


# Section keys → URL hash so the "Take me there" button can deep-link.
# Auto-built from FEATURES keys + sidebar showSection() calls so it stays
# in sync. Override individual mappings here when the URL hash differs
# from the section key (rare).
DEEP_LINKS = {key: f"#section={key}" for key in FEATURES.keys()
              if not FEATURES[key].get("_super_admin_only")}

# Manual overrides for keys where the deep link doesn't match the section name.
DEEP_LINKS.update({
    "profile":       "/profile/",
    "billing":       "/billing/",
    "portals":       "",                 # No single deep link — different URLs per portal
    "couriers":      "#section=orders",  # Couriers are inside the order detail
    "analytics":     "#section=overview",
})
