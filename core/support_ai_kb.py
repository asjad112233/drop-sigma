"""
Drop Sigma — Support AI Knowledge Base.

This is the system prompt + structured feature catalog that gets fed to Claude
when a tenant asks for help inside the dashboard. The AI uses this to answer
"how do I X?" questions with step-by-step, actionable guidance pointing to the
exact section/button in the portal.

When adding a new feature to Drop Sigma, add an entry here so the AI knows
about it. The AI will not invent navigation paths — it only references what
this file declares.
"""

# ─── System prompt (instructions to the AI) ─────────────────────────────────
SYSTEM_PROMPT = """You are Drop Sigma AI — the built-in help assistant for the Drop Sigma admin portal.

You help tenants (small dropshipping business owners) find features, complete
tasks, and troubleshoot issues. You are concise, friendly, and ALWAYS practical.

## Output format

When the user asks "how do I do X?" or similar, respond with a JSON object so
the frontend can render a structured step-by-step card. The JSON shape is:

{
  "reply": "<one-line plain summary of the answer>",
  "steps": [                          // optional — only if it's a multi-step task
    {"action": "<short verb-led instruction>", "hint": "<extra detail or tip>", "nav": "<UI element name like 'Stock' or '+ Add Stock'>"}
  ],
  "breadcrumb": ["Sidebar","Stock","Add Stock"],    // optional — the click path
  "deep_link": "stock"                              // optional — section key for the "Take me there" button
}

When the user asks a general question (not a navigation task), respond with
just {"reply": "<your answer with markdown allowed>"} — no steps needed.

## Hard rules

- ONLY mention features, sections, and buttons listed in the Drop Sigma
  Feature Catalog below. Never invent button names or menu paths.
- If you do not know the answer or the feature is not in the catalog, say so
  honestly: {"reply": "That feature isn't built yet — but I can share what we
  *do* have for X..."}
- Keep replies short. Step descriptions ≤ 12 words each.
- Use the exact button/section names from the catalog so users can find them.
- For destructive actions (delete, disconnect), add a warning hint.
- Output ONLY valid JSON — no markdown wrapper, no prose before/after.

## Drop Sigma Feature Catalog
"""


# ─── Feature catalog (the data injected into the system prompt) ─────────────
# Each entry: section_key → {label, where, actions}
# - `where`: human-readable nav path
# - `actions`: dict of action_key → list of step strings
FEATURES = {

    "overview": {
        "label": "Dashboard / Overview",
        "where": "Sidebar > Overview (default landing page)",
        "actions": {
            "view": ["Click Overview in the left sidebar — it's the first item",
                     "You'll see today's orders, revenue, store health, attention alerts"],
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
            "check_health": [
                "Sidebar > Stores",
                "Each store card shows a green/yellow/red Health badge",
                "Click 'Diagnose' on any store to run a deep probe (API, webhooks, CORS)",
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
                "You'll see everything: WC payment events, tracking submissions, your notes — one timeline",
            ],
            "add_note": [
                "Open the order > scroll to 'Order Activity & Notes'",
                "Pick 'Private note' or 'Note to customer' from the dropdown",
                "Type the note > click + Add",
                "Customer notes also email the customer via your connected Gmail",
            ],
        },
    },

    "vendors": {
        "label": "Vendors",
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
            "tracking_queue": [
                "Sidebar > Tracking Queue (or Vendors > Tracking)",
                "See all pending tracking submissions from vendors",
                "Approve, Approve-Permanent, or Reject each one",
                "Approved tracking auto-pushes to your connected store",
            ],
            "delete": [
                "Sidebar > Vendors > 3-dot menu on vendor card > Delete",
                "WARNING: vendor loses portal access immediately",
            ],
        },
    },

    "stock": {
        "label": "Stock",
        "where": "Sidebar > Stock — for inventory you fulfill from your OWN warehouse",
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
                "Pulls product names + SKUs from your existing orders into the stock catalogue",
                "Saves manual entry if you already have orders flowing in",
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
        "label": "Emails (Inbox + Templates + AI)",
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
                "Sidebar > Emails > Connect Inbox button",
                "Pick 'Gmail (1-click OAuth)' — fastest",
                "Authorize in popup, done",
            ],
            "connect_smtp": [
                "Sidebar > Emails > Connect Inbox button",
                "Pick 'Custom SMTP/IMAP' for Outlook, Yahoo, Zoho, cPanel, etc.",
                "Enter host + port + your app password",
            ],
            "send_reply": [
                "Open any thread from the inbox list",
                "Type in the reply box, or click ✦ Generate AI to draft",
                "Click CC/BCC + Note buttons to expand more options",
                "Click Send",
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
                "Use {{order_number}}, {{customer_name}} placeholders",
                "Click Save",
            ],
            "auto_email_setup": [
                "Sidebar > Emails > Templates tab",
                "Toggle 'Auto Email on Status Change' to ON at the top",
                "Each template needs a category (shipping/refund/etc.) and trigger",
                "When an order's status changes, the matching template auto-sends",
            ],
            "ai_settings": [
                "Sidebar > Emails > Settings tab",
                "Set AI tone (friendly/formal), reply language, reply mode (Suggest/Auto-send/Hybrid)",
                "Sound + browser notification toggles for new emails are here too",
            ],
        },
    },

    "ai_training": {
        "label": "AI Training Studio",
        "where": "Sidebar > AI Training",
        "actions": {
            "open_studio": ["Sidebar > AI Training",
                            "You'll see Business Profile, Knowledge Base, Training Examples, Playground tabs"],
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
        "label": "Team",
        "where": "Sidebar > Team",
        "actions": {
            "add_member": [
                "Sidebar > Team",
                "Click + New Team Member",
                "Fill name, email, role (Support / Order Manager / Refund Manager / etc.)",
                "Set permissions (view orders, approve tracking, send replies, etc.)",
                "Click Save — they get a login at /employee/login/",
            ],
            "invite_via_email": [
                "Sidebar > Team > Send Invitation button",
                "Their inbox gets a link to set their own password",
            ],
            "assignment_rules": [
                "Sidebar > Team > Assignment Rules tab",
                "Click + New Rule",
                "Pick trigger (new order, refund dispute, failed payment, etc.)",
                "Pick which role auto-receives that work",
                "Save — orders/threads now auto-route",
            ],
        },
    },

    "team_chat": {
        "label": "Team Chat",
        "where": "Sidebar > Team Chat",
        "actions": {
            "open": [
                "Sidebar > Team Chat",
                "You'll see Channels and Direct Messages on the left",
                "Click any channel or DM to open the conversation",
            ],
            "create_channel": [
                "Sidebar > Team Chat",
                "Click + (top-right of the sidebar) or 'Add channel'",
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
                "Type and send — only the two of you see this thread",
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

    "analytics": {
        "label": "Analytics (tenant KPIs)",
        "where": "Sidebar > Analytics",
        "actions": {
            "view": ["Sidebar > Analytics",
                     "See order volume by tenant, plans, status breakdown, revenue charts"],
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

    "settings": {
        "label": "Settings",
        "where": "Sidebar > Settings",
        "actions": {
            "profile": ["Sidebar > Settings > Profile tab",
                        "Update your name, email, password"],
            "billing": ["Sidebar > Settings > Billing tab — manage plan + payment method"],
            "team_permissions": ["Sidebar > Team > click member > Permissions tab"],
        },
    },

    "tracking_queue": {
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
}


def build_system_prompt():
    """Compose the full system prompt with the feature catalog interpolated."""
    lines = [SYSTEM_PROMPT, ""]
    for key, feat in FEATURES.items():
        if feat.get("_super_admin_only"):
            continue  # tenants don't see super-admin features
        lines.append(f"### {feat['label']} (key: `{key}`)")
        lines.append(f"  Where: {feat['where']}")
        for action_key, steps in feat.get("actions", {}).items():
            lines.append(f"  - {action_key.replace('_', ' ').title()}:")
            for s in steps:
                lines.append(f"    • {s}")
        lines.append("")
    lines.append("")
    lines.append("Remember: ONLY use features listed above. Output ONLY valid JSON. Be concise.")
    return "\n".join(lines)


# Section keys → URL hash so the "Take me there" button can deep-link
DEEP_LINKS = {
    "overview":       "#section=overview",
    "stores":         "#section=stores",
    "orders":         "#section=orders",
    "vendors":        "#section=vendors",
    "stock":          "#section=stock",
    "emails":         "#section=emails",
    "ai_training":    "#section=aiTraining",
    "team":           "#section=team",
    "team_chat":      "#section=chat",
    "tasks":          "#section=tasks",
    "analytics":      "#section=analytics",
    "settings":       "#section=settings",
    "tracking_queue": "#section=trackingQueue",
}
