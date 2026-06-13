# Drop Sigma — Complete Business Model & Brand Brief

> Hand this document to any designer, developer, agency, or AI tool (Cowork, Webflow, Framer, Vercel v0, etc.) to brief them on Drop Sigma. Everything here is sourced from the actual production codebase.

---

## 1. Elevator Pitch (one sentence)

**Drop Sigma is the operating system for D2C brands — full e-commerce automation (AI auto-replies, order sync, branded tracking, returns) PLUS a verified sourcing network of 200+ suppliers, all managed by your single dedicated manager. One platform replaces 6 SaaS tools and your freight forwarder.**

---

## 2. The Two-Pillar Value Proposition

Drop Sigma's entire value is built on **two equally important pillars**. Every landing page, ad, demo, and onboarding flow must communicate BOTH.

### Pillar 1 — Full Automation (24/7)

| Feature | What it does |
|---|---|
| **AI Auto-Reply** | Reads every customer email, looks up the order data, drafts a reply in the merchant's tone, and **sends automatically** in under 5 seconds. 24/7. Escalates only if confidence < 95%. |
| **Auto Order Sync** | Shopify + WooCommerce webhooks land in the dashboard the second the customer clicks Buy. Real-time, no CSV imports, no polling. |
| **Auto Tracking Push** | Carrier scan events push to the merchant's branded tracking page (`track.dropsigma.com/{tracking-id}`) within 30 seconds. Customers stop emailing "where is my order?" |
| **Auto Returns Desk** | Customer uploads photo → AI categorises the issue → return ticket routes to the supplier automatically → refund triggers on approval. Zero manual work. |
| **Auto Notifications** | Bell-icon notifications for every status change. Live order list refresh every 8 seconds. Real-time chat with typing indicators. |

### Pillar 2 — Verified Sourcing Network

| Feature | What it does |
|---|---|
| **1 Dedicated Manager** | Every tenant is assigned a real human sourcing manager — name, photo, direct chat. **Single point of contact for everything supplier-related.** |
| **200+ Verified Suppliers** | All vetted, KYC'd, with security deposits posted before joining. Production, QC, packaging, dispatch — all in-network. |
| **Zero Supplier Communication** | Tenant NEVER emails, calls, or WhatsApps a supplier. Ever. The dedicated manager + Drop Sigma's 200+ in-house specialist team handle 100% of vendor side. |
| **Single Dashboard Workflow** | Send product spec → manager returns quote → tenant pays from wallet → manager handles vetting, production, QC, packaging, dispatch, last-mile tracking. All in chat. |
| **$0 Lifetime Sourcing Fee** | Drop Sigma charges **nothing** on top of supplier cost. We make money on the SaaS subscription. Aligned incentives — no surprise markups. |

---

## 3. Why Customers Pay Monthly (The Sales Argument)

> *"Pay $99/month for automation that saves $1,268/month."*

Real cost-comparison shown on the landing page:

| Without Drop Sigma | Monthly cost |
|---|---|
| VA for order entry (4hr/day) | −$720 |
| Customer support tool (Helpscout/Gorgias for 6 inboxes) | −$199 |
| Tracking page SaaS (AfterShip / TrackPod) | −$59 |
| Returns helpdesk | −$89 |
| Sourcing agent retainer | −$300 |
| **Subtotal you would pay** | **−$1,367/mo** |
| Drop Sigma Pro plan | +$99/mo |
| **Net monthly savings** | **$1,268/mo** |

Plus you get one platform, one login, one wallet, one manager — instead of 6 tools and 4 freight forwarders.

---

## 4. Target Customer Profile

### Primary persona — "Scaling solo D2C operator"
- Annual revenue: **$200K – $5M**
- Stores: 1–3 (typically Shopify, sometimes WooCommerce)
- Order volume: **150–2,000 orders/month**
- Pain points:
  - Drowning in customer support emails ("where is my order?")
  - Switching between 6 SaaS tools daily
  - Chasing 4 freight forwarders for every restock
  - Working evenings/weekends to keep up
  - Wants to scale but is the bottleneck

### Secondary persona — "Multi-brand operator"
- Annual revenue: **$5M+ across brands**
- Stores: 4–20
- Has 1–3 employees on ops
- Pain: needs unified ops + supplier consolidation across brands

### Anti-persona (NOT for us)
- Massive enterprise (Walmart-scale) — needs custom SLAs we don't offer
- Pure dropshippers (AliExpress arbitrage) — wrong supplier profile
- Service businesses — we're physical-goods-only

---

## 5. Pricing — Three Tiers (source: `superadmin/models.py`)

```python
PLAN_PRICES = {"trial": 0, "basic": 49, "pro": 99, "enterprise": 299}
PLAN_CHOICES = [("basic","Basic"), ("pro","Pro"), ("enterprise","Enterprise")]
```

### Starter — $49/mo
- 1 store · 1 email inbox
- AI auto-reply + order sync + branded tracking + returns desk
- Sourcing network access (self-serve, no dedicated manager)
- *For: single-store operators getting their AI live*

### Pro — $99/mo ★ Most Popular
- 3 stores · 3 inboxes
- Everything in Starter
- **Dedicated sourcing manager** (the big upgrade)
- Priority QC + 24/7 chat
- 3 team seats included
- Custom tracking domain (white-label CNAME)
- *For: brands scaling sourcing + multi-store ops*

### Scale — $299/mo
- Unlimited stores + inboxes
- Everything in Pro
- Custom supplier onboarding
- SSO, audit log, SOC2 docs
- Account manager + SLAs
- API access + webhooks
- *For: multi-brand operators with custom SLAs*

### Billing terms
- **Month-to-month** (no annual contracts)
- **Cancel anytime** (data exports as JSON instantly)
- **No card on signup** for plan selection (Stripe / PayPal at checkout)
- Currency: **USD**

---

## 6. The Subscriber Workflow (How They Use Us)

### Day 1 — Setup (5 minutes)
1. Sign up at `/signup/` with email + password
2. Verify email
3. Connect store (Shopify or WooCommerce OAuth)
4. Sit through the AI Training Studio — answer 50 short questions about tone, refund policy, shipping
5. AI is now ready

### Day 1 — First sourcing interaction
1. Click **Sourcing Partners** in sidebar
2. Auto-assigned a dedicated manager (e.g. "Bryan Buford")
3. Open chat, send product spec or just describe what you need
4. Manager replies within 4 minutes with clarifying questions
5. Get a quote in the dashboard (Pending Payment tab)
6. Pay from Drop Sigma wallet → manager kicks off procurement

### Day 2+ — Steady-state operations
- Customer emails arrive → AI drafts and auto-sends replies in under 5 seconds
- New Shopify orders sync in real-time → appear on dashboard within 1 second
- OPS desk processes the order → tracking added → branded page updates → customer notified
- Tenant just monitors the dashboard. Most days they spend < 30 minutes on ops.

---

## 7. The Tech Stack & Surfaces

### Subscriber-facing apps
1. **Tenant Dashboard** (`/dashboard/`)
   - Overview · Stores · Orders · Emails · Vendors · Tracking Queue · Team · Chat · Sourcing Partners · Stock · Tasks · Returns · AI Training
2. **Branded Tracking Page** (`track.dropsigma.com/<tracking-id>`)
   - Public-facing customer URL
   - Live carrier events, mobile-first
   - White-label CNAME available on Pro+
3. **Employee Portal** (`/employee/dashboard/`)
   - For team members on Pro+ plans
   - Scoped access (only see assigned threads / labels)
4. **Sourcing Partner Workspace** (inside `/dashboard/` → Sourcing Partners)
   - Dedicated chat with manager
   - Orders pipeline: Pending Source → Pending Payment → Processing → Shipping → Delivered
   - Wallet + payments
   - Bulk delete / restore for pending orders

### Internal apps
5. **OPS Portal** (`/ops/`)
   - Drop Sigma's internal team — manages all tenants
   - Queue · Quotes · Procurement · Shipping · Master Catalog · Suppliers · Tenant Chat
6. **Super Admin** (`/superadmin/`)
   - Drop Sigma founder/admin view
   - Tenant management, billing, KPIs, OPS workspaces

### Key automations (always-on)
- **Order webhook ingest** — Shopify + WooCommerce
- **Email sync** — Gmail OAuth, polls every 15s for fresh messages
- **AI reply pipeline** — reads message, fetches order data, drafts in tenant's tone, sends in < 5s
- **Tracking carrier sync** — pulls events from carrier APIs every 5 min
- **Sourcing notifications** — bell-icon ping on every quote/status change
- **Chat polling** — 1.5s for active chat, 3s for sidebar badge

---

## 8. Differentiation (Why Not Helpscout / Gorgias + Freight Forwarder?)

| | Helpscout / Gorgias + freight forwarder | Drop Sigma |
|---|---|---|
| Tools to manage | 4–6 separate SaaS | **1 platform** |
| Supplier communication | You email 4 vendors | **Zero** — your manager handles it |
| Customer support | AI features cost extra | **AI auto-reply included** |
| Tracking page | $59/mo standalone (AfterShip) | **Included + branded** |
| Returns | $89/mo standalone | **Included + auto-routed** |
| Sourcing markup | 5–15% on supplier cost | **$0 lifetime fee** |
| Pricing alignment | Vendor profit from your supply chain | **We profit from SaaS only** |
| Setup time | Weeks (per tool) | **5 minutes** total |

The honest pitch: **"We replace 4 SaaS subscriptions + your freight forwarder for $99/month, and we sleep better because we make money on the SaaS, not on your supplier markup."**

---

## 9. Brand Identity & Voice

### Brand voice
- **Confident, not corporate.** Speak like a smart operator talking to another smart operator.
- **Specific, not fluffy.** "AI replies in 4 seconds" > "Lightning-fast AI"
- **Honest about tradeoffs.** "$0 sourcing fee for life" > "Best rates in the industry"
- **Hindi/Urdu-friendly.** Many subscribers are South-Asian / Middle-Eastern operators. English copy + occasional bilingual touches in dashboards.

### Tone tropes that work
- "Stop running your store. Let Drop Sigma run it."
- "AI auto-replies every customer query. 24 hours a day, every single day."
- "One manager. Zero supplier chaos."
- "Pay for the platform. Sourcing is included."
- "Skip freight forwarders. Skip 6 SaaS tools."

### Tone tropes that DON'T work (avoid)
- "While you sleep" (informal for B2B SaaS)
- "Revolutionary" / "Disrupting" (cliché)
- "Best-in-class" (vague)
- "Unlock your potential" (corporate)

---

## 10. Visual Identity

### Brand colors (exact hex)
```
Primary brand gradient:
  Indigo  #6366F1
  Purple  #A855F7
  Cyan    #06B6D4

Gold accent (Sourcing + featured tiers):
  Gold    #D4AF37
  Gold-2  #FBBF24

Success / live states:
  Green    #10B981
  Green-2  #34D399

Status colors:
  Red      #EF4444   (errors, "deleted" chips)
  Amber    #F59E0B   (warnings)

Light theme inks:
  Ink      #0B1224
  Ink-2    #1E293B
  Ink-3    #475569
  Ink-4    #94A3B8

Dark theme background:
  BG       #06080F
  BG-2     #0B0F1C
  Surface  #11162A
```

### Logo
- **Symbol**: square tile, rounded corners (radius ~22%), gradient fill `indigo → purple → cyan`
- **Wordmark**: "DS" in white, weight 900, letter-spacing −0.5px
- **Files available**:
  - `branding/logo-oauth/google-oauth-best.png` (256×256, solid square)
  - `branding/logo-oauth/dropsigma-logo-rounded-{120,128,192,256,512,1024}.png`
  - `branding/logo-oauth/dropsigma-logo-square-{120,128,192,256,512,1024}.png`

### Typography
- **Display + UI**: `Inter` — weights 400, 500, 600, 700, 800, 900
- **Mono / code**: `JetBrains Mono` — weights 500, 700
- **Both via Google Fonts** (free, CDN)

### UI patterns to repeat across landing pages
- **Glass cards** — white background + `backdrop-filter:blur(20px)` + 1px border
- **Gradient top accent bar** on feature cards (3px tall, full width, brand gradient)
- **Floating particles** — small dots drifting upward, brand-colored
- **Aurora blobs** — radial gradients, large blurred, animated
- **Live pulse dots** — green dot with expanding box-shadow ring, 1.8s loop
- **Hover lift** — cards translateY(-4px) + expanded shadow on hover

### Animations to repeat
- `gradShift` — gradient backgrounds shift position over 8s (used on `.grad` text)
- `livePulse` — green status dots
- `auroraFloat` — slow blob translation 18s
- `floatUp` — particles drift bottom→top 14–22s
- `phoneFloat` — phone mockup rotate slightly, 6s loop
- `drawPath` — SVG stroke-dashoffset for tracking line
- `typingDot` — bouncing dots for chat typing indicators
- `plusPulse` — center "+" badge scales 1 → 1.08 → 1

---

## 11. Existing Code Assets (for the new landing page builder)

### Files to reference
- `templates/home.html` — current production homepage (3,979 lines, too long, to be replaced)
- `home-sample-v2.html` — compact dark-theme version, 612 lines
- `home-sample-v3.html` — comprehensive light-theme version with heavy VFX, 1,800+ lines (current direction)
- `templates/dashboard.html` — full tenant dashboard (UI patterns to reference)
- `templates/_ds_splash.html` — branded loading splash component
- `templates/_ds_components.html` — reusable loader / pager components
- `templates/profile.html` — tenant profile page (with Refer & Earn tab)
- `templates/sourcing_ops/dashboard.html` — OPS-side dashboard
- `branding/icon.png` — source logo, 1024×1024

### Page sections that already exist and are battle-tested
1. **Hero** with twin-pillar callouts (Automation + Suppliers)
2. **Why-pay** ROI calculator
3. **Live Automation Showcase** — 3 demo cards with embedded animations
4. **Sourcing Section** — featuring dedicated manager + chat preview + quote card
5. **Tracking System** — phone mockup with animated SVG path
6. **Comparison table** — with/without Drop Sigma
7. **How it works** — 3 numbered steps
8. **Pricing** — 3 tiers ($49 / $99 / $299)
9. **FAQ** — 6 questions, native `<details>` accordion
10. **Final CTA** — particles + aurora + headline
11. **Footer** — 4-column

---

## 12. Domain & URL Structure

| Surface | URL |
|---|---|
| Marketing site | `https://dropsigma.com/` |
| Tenant dashboard | `https://dropsigma.com/dashboard/` |
| Signup | `https://dropsigma.com/signup/` |
| Login | `https://dropsigma.com/login/` |
| OPS portal | `https://dropsigma.com/ops/` |
| Super admin | `https://dropsigma.com/superadmin/` |
| Branded tracking | `https://track.dropsigma.com/<tracking-id>` |
| Employee portal | `https://dropsigma.com/employee/dashboard/` |
| Referral landing | `https://dropsigma.com/invite/<code>/` |
| Privacy policy | `https://dropsigma.com/privacy/` |
| Terms | `https://dropsigma.com/terms/` |

### Email domains (after Google Group setup)
- Tenant support: `drop-sigma-support@googlegroups.com` (forwards to `support@dropsigma.com`)
- Founder / contact: `support@dropsigma.com`
- OAuth consent screen: shows `drop-sigma-support@googlegroups.com`

---

## 13. Subscriber Growth Mechanics

### Referral program (live in production)
- Every tenant gets a unique invite code via `/api/referrals/my-link/`
- Share URL format: `https://dropsigma.com/invite/<8-char-code>/`
- **3 referrals who SUBSCRIBE (not just sign up) = 3 months free** added to referrer's `trial_ends` and `renews_on`
- Banner on dashboard + dedicated "Refer & Earn" tab in `/profile/`
- Live counter polls `/api/referrals/progress/` every 60s

### Funnel
1. Visitor lands on `/`
2. Reads twin-pillar story → clicks **Get started**
3. Signs up at `/signup/` with email + password
4. Verifies email
5. Lands on `/dashboard/` → connects store
6. Trains AI (~5 min)
7. **Free trial period: 14 days** (model field: `Tenant.trial_ends`)
8. Upgrades to paid plan via `/upgrade/` → Stripe / PayPal checkout
9. Plan flips to active, `Tenant.status = "active"`

### Key conversion metric
**Time-to-first-quote** (signup → first sourcing quote received from manager) — target: < 24 hours. This is the moment subscribers feel the platform is worth $99/month.

---

## 14. What to Build (Brief for the Landing Page Designer)

### Mandatory page sections (in order)
1. **Sticky nav** with glass blur
2. **Hero** — twin-pillar callouts (Automation + Suppliers), both equally prominent, with animated `+` connector
3. **Trust strip** — 4 metric numbers (orders/day, AI replies, specialists, uptime)
4. **Why-pay** — ROI calculator card showing $1,268/mo savings
5. **Live Automation showcase** — 3 demo cards: Order Sync · AI Auto-Reply · Live Tracking
6. **Sourcing Partners** — dedicated manager hero, chat preview (tenant ↔ manager), quote card. **NO vendor / supplier names anywhere.**
7. **Tracking System** — phone mockup with track.dropsigma.com, animated route SVG
8. **Comparison** — Without vs With Drop Sigma (red/dark)
9. **How it works** — 3 steps (Connect store / Train AI / Source & ship)
10. **Pricing** — 3 tiers, Pro featured with gold accent
11. **FAQ** — 6 questions, accordion
12. **Final CTA** — aurora + particles
13. **Footer** — 4 columns, social icons

### Mandatory copy beats
- Hero must mention **AI auto-replies 24/7** AND **200+ verified suppliers** in the first paragraph
- Pricing section must say **"Sourcing is included. $0 lifetime fee."**
- Sourcing section must say **"One manager. Zero supplier chaos."** and **"You never email a supplier."**
- Tracking section must show `track.dropsigma.com/<id>` URL prominently
- Comparison must show 6 bullet pairs (manual pain vs Drop Sigma fix)

### Mandatory CTAs
- Primary: **"Get started →"** (links to `/signup/`)
- Secondary: **"See it work"** (jumps to #automation section)
- Pricing CTAs: **"Subscribe to Starter / Pro / Talk to sales"** (NO "free trial" wording)

### Banned terminology
- ❌ "while you sleep"
- ❌ "free trial" (we don't market it; users get 14-day trial automatically after email verification but it's not a sales angle)
- ❌ "no credit card required"
- ❌ Any specific supplier or vendor name (no "German Drop", "Shenzhen Wang", etc.)
- ❌ "Revolutionary", "Disrupting", "Best-in-class"

---

## 15. The 30-Second Pitch (Memorize This)

> Drop Sigma is one platform for D2C ops. **Automation:** AI auto-replies to every customer query in 5 seconds, orders sync from Shopify and WooCommerce in real-time, and a branded tracking page handles "where is my order?" emails automatically. **Sourcing:** a dedicated manager and 200+ verified suppliers handle your entire supply chain — you never email a vendor again. Pro plan is $99/month, sourcing is included with zero markup, month-to-month with no contracts. It replaces 4 SaaS tools and your freight forwarder.

---

**End of document.** Anyone building a Drop Sigma surface — landing page, ad, deck, voiceover, brochure — should be able to work from this brief alone.
