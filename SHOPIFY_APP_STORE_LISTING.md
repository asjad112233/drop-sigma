# Drop Sigma — Shopify App Store Listing Content

This document contains all the copy + assets needed to submit Drop Sigma to the Shopify App Store.
Copy-paste each field into the matching input in **Partner Dashboard → Drop Sigma → App Store listing**.

---

## 📌 App Identity

| Field | Value |
|---|---|
| **App name** | Drop Sigma |
| **App handle (URL slug)** | drop-sigma |
| **Tagline** (60 chars max) | All-in-one dropshipping OS — orders, AI email, team |
| **Subtagline** (100 chars) | Sync orders, manage vendors, auto-reply with AI — built for dropshippers |
| **Primary category** | Orders and shipping |
| **Secondary category** | Productivity |

---

## 📝 Long Description (1500–2000 chars)

> Copy this into the **"App description"** field. Markdown-style formatting renders correctly on the App Store.

```
Drop Sigma is the all-in-one operations platform built for Shopify dropshippers and DTC brands. Instead of juggling 5 different tools — order CRM, vendor coordinator, email helpdesk, AI replier, tracking pusher — Drop Sigma unifies them in a single dashboard so your team ships faster and your customers stay happy.

KEY FEATURES

✦ Real-time order sync
Connect your store in one click. Every new order, payment update, fulfillment event, and cancellation streams into Drop Sigma within seconds via Shopify webhooks. No manual exports, no missed orders.

✦ AI-powered customer email
Drop Sigma reads incoming customer emails (Gmail, IMAP), classifies the topic (refund, return, shipping, dispute, etc.), and drafts a reply using your brand voice + the policies you train it on. Review before sending or enable auto-reply for high-confidence categories. Cut support response times by 80%.

✦ Vendor & fulfillment workflow
Assign orders to vendors with a single click. Vendors get their own portal where they see only their assigned orders, submit tracking numbers, and mark fulfilled. Approve or reject from your admin dashboard. Tracking pushes back to Shopify automatically.

✦ Multi-portal access control
Three purpose-built portals: Admin (you), Employee (your support team), Vendor (your suppliers). Granular permissions per role — give your support team access to specific folders and labels, give vendors access only to their orders. Fully GDPR-compliant per-tenant data isolation.

✦ Smart tracking
Pull live tracking status from 1500+ couriers. Auto-detect delivery delays. Notify customers proactively when their package gets stuck.

✦ Team chat & activity log
Built-in team chat per channel + complete audit log for every thread (who replied when, who assigned what). Perfect for distributed teams across time zones.

WHO IS DROP SIGMA FOR?

• Dropshippers running 100–10,000 orders/month
• DTC brands with 2–20 person support teams
• Multi-store merchants who want one inbox across all stores
• Operators tired of context-switching between Gmail, Shopify, vendor spreadsheets

PRICING

Free 14-day trial — no card required. Plans start at $49/month for up to 500 orders.

SUPPORT

Questions? Email support@dropsigma.com — our team replies within 4 hours on business days. We also offer onboarding sessions for teams that need help migrating from another tool.

Drop Sigma was built by dropshippers, for dropshippers. Try it free for 14 days and see your support team breathe again.
```

---

## 📸 Required Visual Assets

### **1. App icon** — `512 × 512 px` PNG (also `1200 × 1200 px` for HD)

**Design brief:**
- Background: Purple-to-pink gradient (`#6366f1 → #a855f7`)
- Foreground: "DS" monogram in bold white, OR a stylized package/box icon
- No transparent background (App Store requires solid)
- 24 px safe padding from edges
- Save as: `drop-sigma-icon-1200.png`

**Tool**: Canva → Custom size 1200×1200 → upload Drop Sigma logo → export PNG.

### **2. Feature banner** — `1920 × 1080 px` PNG

Goes at the top of the listing. Brand-forward, no real screenshots.
- Left half: "Drop Sigma" wordmark + tagline
- Right half: Dashboard mockup or abstract gradient
- Save as: `drop-sigma-banner-1920.png`

### **3. Screenshots** — 5 images at `1280 × 800 px` PNG

Capture these flows from your live dashboard:

| # | Screen | Caption |
|---|---|---|
| 1 | Overview / KPI dashboard | "See every order across every store at a glance" |
| 2 | Orders list with vendor assignment chips | "Assign orders to vendors in one click" |
| 3 | Email inbox with AI Draft chip on a thread | "AI drafts customer replies in your brand voice" |
| 4 | AI Training Studio / topics page | "Train the AI on your policies — never invent answers" |
| 5 | Team portal / employee view | "Granular permissions for your support team" |

**Tips**:
- Use real (but redacted) data — e.g., real customer names blurred or replaced with "Jane S.", "Alex M."
- Browser full-screen (Cmd+Shift+F on Mac) — no URL bar or tabs visible
- Resolution exactly 1280×800 (use macOS Preview → Tools → Adjust Size if needed)
- File names: `drop-sigma-screen-1-overview.png`, ..., `-5-team.png`

### **4. Demo video** — 30–180 seconds, MP4, ≤ 10 MB

Loom recording of the full happy path:
1. Connect a Shopify store (Auto Connect → install)
2. Watch first orders sync in
3. Open an email, AI drafts a reply, you click Send
4. Assign an order to a vendor
5. Vendor logs in to their portal and submits tracking
6. Tracking pushes back to Shopify

Upload to YouTube as **unlisted** and provide the URL in the listing. Add a 5-second intro card with "Drop Sigma" branding.

---

## 🔗 Required URLs

These all live on `dropsigma.com` and are already coded in the Django project:

| Field | URL |
|---|---|
| **App URL** | https://dropsigma.com/dashboard/ |
| **Privacy policy** | https://dropsigma.com/privacy/ |
| **Terms of service** | https://dropsigma.com/terms/ |
| **Support / FAQ** | https://dropsigma.com/support/ |
| **Marketing site** | https://dropsigma.com/ |

---

## 🔧 Configuration Settings (Partner Dashboard)

### **Allowed redirection URL(s)** (must match `core/views.py` exactly)

```
https://dropsigma.com/stores/api/shopify-callback/
http://127.0.0.1:8000/stores/api/shopify-callback/
```

### **Compliance webhooks** (set in Partner Dashboard → Configuration → Compliance webhooks)

| Topic | URL |
|---|---|
| **Customer data request** | https://dropsigma.com/orders/webhook/shopify/customers-data-request/ |
| **Customer redact** | https://dropsigma.com/orders/webhook/shopify/customers-redact/ |
| **Shop redact** | https://dropsigma.com/orders/webhook/shopify/shop-redact/ |

### **App uninstall webhook** (set in Webhooks section)

| Topic | URL | API version |
|---|---|---|
| **app/uninstalled** | https://dropsigma.com/orders/webhook/shopify/app-uninstalled/ | 2024-01 or later |

### **Access scopes** (set in API access)

```
read_orders, write_orders, read_customers, write_customers, read_products, read_fulfillments, write_fulfillments
```

### **App contact info**

| Field | Value |
|---|---|
| Emergency contact | support@dropsigma.com |
| API contact | dev@dropsigma.com (or your personal email) |

---

## 💰 Pricing Plan (App Store listing pricing card)

Shopify expects a clear pricing snippet on the listing. Recommended structure:

| Plan | Price | What's included |
|---|---|---|
| **Free Trial** | $0 — 14 days | Everything in Growth |
| **Growth** | $49 /month | Up to 500 orders/month · 3 team members · 1 store |
| **Scale** | $149 /month | Up to 5,000 orders/month · 10 team members · 5 stores · AI replies |
| **Plus** | $399 /month | Unlimited orders/team/stores · Priority support · SSO |

(Adjust to your real pricing — these are placeholders that match Pakistani SaaS norms.)

---

## ✅ Pre-Submission Checklist

Before clicking **"Submit for review"** in Partner Dashboard, verify:

- [ ] App icon uploaded (1200×1200 PNG)
- [ ] Banner image uploaded (1920×1080 PNG)
- [ ] 5 screenshots uploaded (1280×800 PNG each)
- [ ] Demo video URL added (YouTube unlisted)
- [ ] App description filled (1500+ chars)
- [ ] Tagline + subtagline filled
- [ ] Privacy policy URL works: https://dropsigma.com/privacy/
- [ ] Terms URL works: https://dropsigma.com/terms/
- [ ] Support URL works: https://dropsigma.com/support/
- [ ] Allowed redirect URL matches Django route
- [ ] All 4 compliance webhooks configured + responding 200
- [ ] App installed + tested on at least 2 development stores
- [ ] Order sync tested (create a test order, watch it appear in Drop Sigma)
- [ ] Uninstall tested (uninstall from dev store, verify Store row deactivates)
- [ ] Pricing plan defined
- [ ] Categories selected (Orders & shipping + Productivity)
- [ ] Built for Shopify standards reviewed (https://shopify.dev/docs/apps/store/requirements)

---

## 📤 Submission Flow

1. **Partner Dashboard** → Apps → **Drop Sigma**
2. **Distribution** → change from "Custom distribution" to **"Public app"**
3. Fill **App Store listing** form with everything above
4. **App review** tab → answer the questionnaire honestly:
   - "Does your app use webhooks?" → **Yes** (orders + GDPR)
   - "Does your app handle customer data?" → **Yes** (with consent)
   - "Does your app integrate with third parties?" → **Yes** (AI providers, email)
5. Click **"Submit for review"**

**Expected timeline**: 5–10 business days for first review. Shopify usually requests 1–2 small changes; address them and resubmit. Final approval typically within 2 weeks.

---

## 🎉 After Approval

Once approved, the app goes live at:
```
https://apps.shopify.com/drop-sigma
```

Merchants can install with one click from there, OR from inside Drop Sigma's "Auto Connect Shopify" dashboard (which already builds the right OAuth URL). All multi-tenant flows work automatically.

You can also keep a "Custom distribution" install link for high-value clients you want to onboard before they install from the Store.

---

## 📞 Support contact emails to set up

Make sure these inbox addresses exist (forward to your personal Gmail if you don't have hosted email yet):

- `support@dropsigma.com`
- `privacy@dropsigma.com`
- `legal@dropsigma.com`
- `feedback@dropsigma.com`

Most domain registrars (Namecheap, GoDaddy, Cloudflare) offer free email forwarding — set these up in 5 minutes.

---

**Questions?** Refer back to `/Users/muhammadasjad/Drop Sigma/docs/shopify-setup.md` or ping the engineering team.
