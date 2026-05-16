# Drop Sigma — Super Admin Panel Brief

## Project Overview

**Drop Sigma** ek **SaaS Ecommerce Operations OS** hai jo dropshipping businesses ko sell hota hai as a subscription.

**Tech Stack:** Django (Python) + PostgreSQL + WhiteNoise + Railway (hosting)  
**Live URL:** `https://dropsigma.com`  
**Current Portals:**
- `/dashboard/` → Admin Dashboard (store owner)
- `/vendor/dashboard/` → Vendor Portal
- `/employee/dashboard/` → Employee Portal

---

## Super Admin Ka Role

Super Admin = **Product Owner (Tum)**  
Tumhare neeche **Tenants** hain — har tenant ek paying subscriber hai jo VendorFlow AI use karta hai apne business ke liye.

```
SUPER ADMIN (You)
    ├── Tenant A — Kayali Store (Subscribed: Pro Plan, $99/mo)
    ├── Tenant B — ABC Dropship (Subscribed: Basic, $49/mo)
    ├── Tenant C — XYZ Fashion (Trial, expires 15 May)
    └── Tenant D — New signup today
```

---

## Current System Architecture

### Apps / Modules

| App | Purpose |
|-----|---------|
| `stores` | WooCommerce/Shopify store connections |
| `orders` | Order management, tracking, assignment |
| `vendors` | Vendor accounts, assignments, tracking queue |
| `teamapp` | Team members, roles, assignment rules, chat |
| `emails` | Gmail inbox, email templates, AI drafts |
| `stock` | Inventory management, stock orders |
| `ai` | AI email drafts |
| `coreapp` | Auth, login |

### Key Models

**Store**
```python
Store(user, name, platform, store_url, api_key, api_secret, access_token, is_active)
```

**Order**
```python
Order(store, external_order_id, customer_name, customer_email, total_price,
      fulfillment_status, payment_status, tracking_number, tracking_company,
      tracking_url, tracking_status, assigned_vendor, assigned_to, vendor_status)
```

**Vendor**
```python
Vendor(user, name, email, phone, company_name, password_plain, status,
       assigned_store, permissions, notes)
```

**TeamMember**
```python
TeamMember(user, name, email, role, status, workload, is_active, permissions)
```

**Stock Models**
```python
StockProduct(store, product_id, product_name, image_url, is_active)
StockVariant(product, color, size, sku)
StockEntry(variant, quantity, reserved, updated_by)
StockOrderAssignment(order, variant, product_id, quantity)
StockAutoRule(store, product_id, variant)
```

---

## Super Admin Panel — Kya Chahiye

### 1. Tenants Management
- **Tenant list** — Har subscriber ka naam, email, plan, join date, status (active/trial/suspended)
- **Tenant detail** — Us tenant ke stores, orders count, vendors count, team members count
- **Create new tenant** — Manually onboard karo kisi ko
- **Suspend / Activate** — Access rokna ya dena
- **Login as Tenant** — Super admin kisi bhi tenant ke dashboard mein ja sake (impersonation)

### 2. Subscriptions & Billing
- **Plans:** Free Trial / Basic ($49/mo) / Pro ($99/mo) / Enterprise (custom)
- **Each tenant ka plan** — Kaunsa plan, kab renew hoga, payment status
- **MRR (Monthly Recurring Revenue)** — Total revenue dashboard
- **Revenue chart** — Last 30/90 days
- **Failed payments** — Jinke payment fail hogaye
- **Upcoming renewals** — Agle 7 days mein kitne renew honge

### 3. Usage Analytics
- **Per tenant usage:**
  - Total orders processed (lifetime + this month)
  - Stores connected
  - Vendors added
  - Team members
  - Emails sent
  - AI calls used
- **Global stats:**
  - Total active tenants
  - Total orders across all tenants
  - Total revenue processed through platform

### 4. Activity & Audit Log
- Kaunse tenant ne kya kiya — store connected, vendor added, etc.
- System errors per tenant

### 5. Support / Notes
- Admin notes per tenant
- Flag a tenant for follow-up

---

## Super Admin Panel — UI Pages Needed

### Page 1: Dashboard (Home)
```
┌─────────────────────────────────────────────────┐
│  SUPER ADMIN — Drop Sigma                        │
├──────────┬──────────┬──────────┬────────────────┤
│ 12       │ $1,188   │ 3        │ 2              │
│ TENANTS  │ MRR      │ TRIALS   │ EXPIRING SOON  │
├──────────┴──────────┴──────────┴────────────────┤
│  Revenue Chart (Last 30 Days)                   │
├─────────────────────────────────────────────────┤
│  Recent Signups        │  Recent Activity        │
└─────────────────────────────────────────────────┘
```

### Page 2: Tenants List
```
┌──────┬─────────────┬──────────┬───────┬─────────┬──────────┐
│ Name │ Email       │ Plan     │ Stores│ Orders  │ Actions  │
├──────┼─────────────┼──────────┼───────┼─────────┼──────────┤
│ Asj..│ asjad@..   │ Pro      │ 1     │ 50      │ View Login│
│ ABC..│ abc@...    │ Basic    │ 2     │ 120     │ View Login│
│ XYZ..│ xyz@...    │ Trial ⚠️ │ 1     │ 8       │ View Login│
└──────┴─────────────┴──────────┴───────┴─────────┴──────────┘
```

### Page 3: Tenant Detail
- Tenant info (name, email, plan, dates)
- Their stores list
- Order stats (this month vs last month)
- Vendor count, team count
- Activity timeline
- Admin notes section
- Danger zone: Suspend / Delete

### Page 4: Subscriptions / Billing
- All subscriptions table
- Filter by plan / status
- MRR breakdown
- Failed payments alert

---

## What Needs to Be Built

### Backend (Django)

1. **`superadmin` app** — New Django app
2. **`Tenant` model** — Maps to a Django `User`, has plan, status, billing info
3. **`Subscription` model** — Plan, price, start_date, end_date, status, payment_status
4. **`TenantActivity` model** — Log of actions per tenant
5. **Super admin middleware** — Only `is_superuser=True` users can access `/superadmin/`
6. **Tenant impersonation** — Super admin can login as any tenant
7. **API endpoints** — List tenants, tenant detail, update plan, suspend, etc.

### Frontend (HTML/CSS/JS)

- `/superadmin/` — Super Admin Dashboard
- Same design language as existing portal (dark sidebar, cards, tables)
- Completely separate from tenant dashboard

---

## Existing Design Reference

The existing dashboard (`/dashboard/`) uses:
- **Dark sidebar** (`#0f172a` background) with white text
- **CSS variables:** `--vf-surface`, `--vf-text`, `--vf-muted`, `--vf-line`, `--vf-primary`
- **Cards** with `border-radius: 20-26px`, subtle shadow
- **Gradient accents:** `linear-gradient(135deg, #35cfff, #8b5cf6)`
- **Tables** with sticky headers, hover effects
- **Font:** System font stack, weights 700-900 for headings

The Super Admin portal should have a **similar but distinct** look — maybe slightly different accent color (e.g. orange/gold for "owner" feel) to distinguish it from the tenant dashboard.

---

## Access Control

- Super Admin URL: `/superadmin/` 
- Only accessible if `request.user.is_superuser == True`
- Tenants CANNOT access this URL
- Super Admin CAN access tenant dashboards via impersonation

---

## Summary — Kya Banana Hai

**Ek alag panel** jo sirf tum (super admin) use karo jahan:
1. Sab tenants dikhen (koi bhi subscriber)
2. Har ka plan, payment status, usage dikhe
3. Naya tenant add kar sako
4. Kisi ko suspend / activate kar sako
5. Revenue / MRR track kar sako
6. Kisi bhi tenant ke dashboard mein ja sako (impersonation)

**Stack:** Django backend + single HTML page (same pattern as existing dashboard.html)
