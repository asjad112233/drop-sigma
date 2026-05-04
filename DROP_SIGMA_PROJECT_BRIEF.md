# Drop Sigma — Complete Project Brief
## (For Homepage Design Reference)

---

## 1. What Is Drop Sigma?

**Drop Sigma** is a **full-stack Ecommerce Operations OS** built specifically for **dropshipping businesses**.

It is a **private SaaS admin panel** — not a public marketplace. A dropshipping store owner logs in, connects their WooCommerce or Shopify store, and manages every operational function from a single dashboard:

- Order intake & assignment
- Vendor coordination
- Team management
- Customer email support
- Automated shipping emails
- Live tracking sync with courier websites
- AI-powered email drafts

**Live URL:** `https://baghawat.com`  
**Tech Stack:** Django (Python), PostgreSQL, WhiteNoise, Railway (hosting)  
**Integrations:** WooCommerce API, Shopify API, Gmail OAuth2, OpenAI / Anthropic AI, Playwright (live tracking scraper)

---

## 2. Three User Portals

| Portal | URL | Who Uses It |
|--------|-----|-------------|
| **Admin Dashboard** | `/dashboard/` | Store owner — full access |
| **Vendor Portal** | `/vendor/dashboard/` | Vendor (supplier) — limited access |
| **Employee Portal** | `/employee/dashboard/` | Team member — role-based access |

Login page: `/login/` — unified page with tabs for Admin / Vendor / Team

---

## 3. Core Modules (Apps)

### 3.1 Stores
- Connect **WooCommerce** or **Shopify** stores via API key + secret
- **Live webhook sync** — orders push to Drop Sigma in real time when placed on the store
- **Store Health Monitor** — real-time API connection check; alerts if store goes offline
- Switch between multiple connected stores via a project switcher in the sidebar
- Per-store data isolation (orders, vendors, emails, templates all scoped per store)

### 3.2 Order Management
- All orders from all connected stores visible in one unified table
- **Smart Filters:** by status (processing, shipped, completed, failed, cancelled), by date, by vendor, by customer name, by product
- **Bulk Actions:** assign vendor in bulk, update status in bulk
- **Order Timeline / Activity Log:** every action on an order is logged (assigned, vendor assigned, tracking submitted, approved, etc.)
- Payment status, fulfillment status, tracking number, tracking company all tracked
- Orders can be assigned to a **Team Member** or a **Vendor**
- Real-time polling — new orders appear automatically every 30 seconds

### 3.3 Vendor Management
- Create vendor accounts with name, email, phone, company, country
- Assign vendors to an entire store OR to specific products
- **Vendor Portal** — vendors get their own login, see only their assigned orders
- Vendor sees: customer name, product, order ID, and can submit a tracking number
- **Granular Permissions per vendor:**
  - Show/hide customer email
  - Show/hide order amount
  - Show/hide assigned team member
  - Show/hide store URL
- **Permanent Product Assignment:** lock a product to a vendor permanently — every new order for that product auto-routes to that vendor with zero manual work
- **Tracking Approval Queue:** vendor submits tracking → admin reviews and approves
- **Auto-Approve toggle** per store or per product — skip the queue entirely for trusted vendors
- Vendor status: Active / Inactive
- Copy vendor login credentials with one click
- Reset vendor password from admin panel

### 3.4 Tracking Approval Queue
- Vendors submit: tracking number, tracking URL, courier name, vendor note
- Admin sees a queue of all pending submissions
- Admin can: **Approve** (standard), **Approve Permanently** (auto-approve this product forever), or **Reject** with a reason
- Once approved, the tracking number is saved to the WooCommerce order
- **Auto-approve toggle** — turn on per store to skip manual review

### 3.5 Live Tracking Sync (Unique Feature)
- Once a tracking number is approved, Drop Sigma **polls the courier website live** using Playwright (headless Chromium)
- Scrapes the live delivery status from the courier's tracking page
- **Auto-updates the WooCommerce order status** based on live tracking:
  - Tracking received → Processing → Shipped → Out for Delivery → Delivered
- `live_tracking_status` field stored on each order
- `delivered_at` timestamp recorded when delivery is confirmed
- Supports any courier — uses pattern-matching on tracking URLs
- Status keywords detected: delivered, in transit, out for delivery, customs, exception, returned, etc.
- Admin can manually trigger a live tracking fetch from the order detail page

### 3.6 Gmail Inbox Integration
- One-click **Google OAuth2** connection — no app password needed
- Connect one or more Gmail inboxes per store
- **Chat-style inbox UI** — emails displayed as conversation threads
- Folder view: Inbox, Sent, Drafts with unread counts
- Email categories: Refund, Shipping, General, Dispute, Order
- **Thread assignment** — assign an email thread to a team member
- Real-time sync (auto-polls every N seconds, configurable)
- Mark as read, reply, send new email — all inside Drop Sigma
- Email linked to orders automatically

### 3.7 AI Email Drafts
- When viewing a customer email, click **"AI Draft"** — AI reads:
  - The customer's email content
  - The linked order details (order number, product, status, tracking)
  - Your configured tone preference
- Generates a **ready-to-send reply** inside the reply box
- **Settings per inbox:**
  - Tone: Friendly / Professional / Formal
  - Language: English / French / Arabic / etc.
  - Auto-suggest: show draft automatically when email opens
  - Auto-draft: draft is generated silently in background
  - Include order context: toggle on/off

### 3.8 Automated Email Templates
- Fully visual **drag-and-drop style** template editor
- Template categories:
  - Order Confirmation (Processing)
  - Shipping Notification
  - Order Completed
  - Payment Failed
  - Order Cancelled
  - Dispute
  - Welcome / Follow-up / Custom
- **Trigger types:**
  - Manual only
  - Order placed
  - Tracking added
  - Order cancelled
  - Payment failed
  - Order delivered
  - 7 days no activity
- **Dynamic variables** in templates: `{{customer_name}}`, `{{order_number}}`, `{{product_name}}`, `{{tracking_number}}`, `{{store_name}}`, etc.
- **Auto Email on Status Change toggle** — one switch enables automatic sending of the default template when order status changes
- Multiple template designs per category — mark one as Default
- Set default templates per category — auto-trigger uses the default
- 10+ pre-built template designs included out of the box

### 3.9 Team Management
- Create team member accounts with:
  - Name, email, role, status (Available / Busy / Limited / Offline)
- **Roles:**
  - Support
  - Order Manager
  - Refund Manager
  - Vendor Manager
  - Email Manager
- **Granular permissions per team member** (40+ toggleable permissions):
  - View/edit orders, vendors, stores, emails, tracking
  - Approve tracking, manage templates, invite members, etc.
- **Team Workload view** — see each member's assigned order count
- Orders assigned to specific team members
- Email threads assigned to team members
- Employee Portal — team members log in separately, see only what they're permitted

### 3.10 Revenue & KPI Dashboard (Overview)
- KPI cards on login:
  - Total Orders (+ today's count)
  - Total Revenue (+ today's revenue)
  - Active Vendors
  - Need Attention (unassigned orders + missing tracking)
  - Unread Emails
- **7-day Revenue Chart** — bar chart with daily revenue and order count
- **Store Health panel** — shows connection status of each store
- **Top Vendors panel** — ranked by order count with progress bars
- **Tracking Queue widget** — shows pending approvals count
- **Recent Orders table** — last 10 orders with status badges
- **Recent Emails widget** — latest unread customer emails
- Alert banner — warns when orders need vendor assignment or tracking

---

## 4. Pricing Tiers

### Starter — $49/month
**For:** Solo dropshippers, 1 store
- 1 Store (WooCommerce or Shopify)
- Order Management & Sync
- Up to 3 Vendors + Vendor Portal
- Tracking Approval Queue
- Live Tracking Sync (Basic)
- Gmail Inbox Integration
- 5 Email Templates
- Store Health Monitor
- 1 Team Member
- ~~AI Email Drafts~~
- ~~Auto Email on Status Change~~
- ~~Multi-Store Support~~

### Growth — $100/month ⭐ Most Popular
**For:** Growing businesses, multiple stores & vendors
- Up to 3 Stores
- Unlimited Vendors + Permanent Product Assignment
- Up to 5 Team Members + Role-Based Permissions
- Live Tracking Sync (Auto Status Update)
- Unlimited Email Templates
- Auto Email on Status Change
- AI Email Drafts (GPT-Powered)
- Revenue & KPI Dashboard
- Store Health Monitor
- Bulk Order Actions
- ~~Dedicated Account Manager~~
- ~~Custom Email Branding~~
- ~~Priority 24/7 Support~~

### Scale — $150/month
**For:** Full-scale operations, agencies, white-label
- Unlimited Stores
- Unlimited Vendors & Team Members
- Live Tracking Sync + Auto-Approve per Product
- Dedicated Account Manager
- Custom Email Branding
- Priority Support (24/7)
- Advanced Analytics & Reports
- Custom Integrations on Request
- Multi-Currency Support
- Onboarding & Setup Assistance
- Vendor Workload Balancing
- White-Label Option

---

## 5. Unique Selling Points (For Homepage Copy)

1. **Everything in one tab** — no switching between Shopify, Gmail, courier websites, WhatsApp. Every operation is inside Drop Sigma.
2. **Live Tracking Sync** — industry-rare feature: system polls courier sites live and auto-updates WooCommerce order status without any manual work.
3. **Vendor Portal** — vendors log in separately, submit tracking, never see what they shouldn't.
4. **AI that knows your orders** — unlike generic AI tools, the AI reads the actual order data when drafting replies.
5. **Auto Email Machine** — one toggle and every status change triggers a branded email to the customer automatically.
6. **Built for scale** — works for 1 store or 10 stores, 1 vendor or 100 vendors, all from the same interface.
7. **30-day persistent sessions** — team members stay logged in without disruption.

---

## 6. Target Audience

- **Dropshipping store owners** (WooCommerce / Shopify) who manage suppliers and customer support
- **Small ecommerce agencies** managing multiple client stores
- **Growing dropshipping operations** needing team structure and vendor coordination

---

## 7. Brand Identity

- **Brand Name:** Drop Sigma
- **Tagline:** Ecommerce Operations OS
- **Domain:** baghawat.com
- **Color feel:** Dark, premium, modern SaaS — deep navy/indigo + purple gradient accents + cyan highlights
- **Font:** Inter (Google Fonts)
- **Logo mark:** "DS" in a purple-indigo gradient rounded square

---

## 8. Homepage Sections (Suggested)

1. **Nav** — Logo + Features / How it Works / Pricing + Login button
2. **Hero** — Bold headline, subheadline, CTA button, dashboard mockup/screenshot
3. **Marquee strip** — scrolling feature names
4. **Stats** — e.g. "12 Features · AI-Powered · Multi-Store · 3 Portals"
5. **Features Grid (12 cards):**
   - Multi-Store Management
   - Order Management
   - Vendor Management
   - Live Tracking Sync ⭐ (highlight this)
   - Tracking Approval Queue
   - Gmail Inbox Integration
   - AI Email Drafts
   - Automated Email Templates
   - Team Management
   - Revenue & KPI Dashboard
   - Vendor Permanent Products
   - Store Health Monitor
6. **How It Works** — 5 steps: Connect Store → New Order Syncs → Assign to Vendor → Vendor Submits Tracking → Customer Gets Email
7. **Pricing** — 3 tiers ($49 / $100 / $150)
8. **Portals section** — Admin / Vendor / Team (3 cards)
9. **CTA banner** — "Start managing smarter"
10. **Footer**

---

## 9. Current Tech Details (For Context)

- **Backend:** Django 4.x, Django REST Framework
- **Database:** PostgreSQL (Railway) / SQLite (local)
- **Auth:** Django session auth + CSRF-exempt DRF for SPA
- **Email:** Gmail API via OAuth2 (IMAP/SMTP fallback)
- **AI:** OpenAI GPT + Anthropic Claude (both keys configured)
- **Live Tracking:** Playwright (headless Chromium) scraping courier websites
- **Hosting:** Railway (auto-deploy from GitHub)
- **Static files:** WhiteNoise + CompressedManifestStaticFilesStorage
- **Sessions:** 30-day persistent cookies
- **Webhooks:** WooCommerce & Shopify webhooks for real-time order sync

---

*This file was generated to brief a homepage designer/AI on the full scope of the Drop Sigma platform.*
