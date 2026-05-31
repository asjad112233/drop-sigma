# Shopify OAuth Scope Justification — Drop Sigma

This document provides per-scope justification for the Shopify Admin API access requested by Drop Sigma during OAuth install. It is intended to be pasted, scope-by-scope, into the "Why do you need this?" fields of the Shopify Partner Dashboard app submission form.

**App:** Drop Sigma (https://dropsigma.com)
**App type:** Public, multi-tenant SaaS
**Purpose:** Help Shopify merchants automate customer email replies (AI-assisted), manage RMAs (returns / refunds), and route vendor assignments — all from a unified operations dashboard outside the Shopify admin.

**Requested scopes (from `core/settings.py`):**

```
read_orders, write_orders, read_customers, write_customers,
read_products, read_fulfillments, write_fulfillments
```

---

### `read_orders`

**Why we need it.** Order data is the backbone of every Drop Sigma feature — without it, we cannot link incoming customer emails to the order they're asking about, render an RMA workflow, or give the AI agent the context it needs to draft an accurate reply. Read access lets us surface relevant order details inside Drop Sigma so merchants don't have to flip back to the Shopify admin mid-conversation.

**Specific features that depend on it:**

- Render the order timeline (status, line items, totals, shipping address) inside Drop Sigma's inbox threads so support agents have full context next to the customer's message.
- Populate refund / RMA request templates with the correct `order_id`, order total, currency, and line items so merchants don't fat-finger refund amounts.
- Automatically link an incoming customer email to the right Shopify order by matching the sender's email address against the order's customer record (eliminates manual order lookup for every reply).
- Provide the AI email-drafting agent with order context (product names, quantities, fulfillment state) so its drafts reference the actual order rather than hallucinating details.
- Power Drop Sigma's revenue dashboards, order-volume KPIs, and store-health monitoring.

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

### `write_orders`

**Why we need it.** When a merchant approves a refund or completes an RMA inside Drop Sigma, the order metadata in Shopify must be updated to reflect that outcome — otherwise the merchant's Shopify Orders dashboard drifts out of sync with reality and customers get conflicting status across channels. Write access keeps both systems in lockstep so Shopify remains the source of truth for fulfillment / refund state.

**Specific features that depend on it:**

- Push the new fulfillment status (e.g. set order to `refunded` / partially refunded) back to Shopify after a Drop Sigma RMA is approved, so the merchant's Shopify admin reflects the same outcome.
- Add internal order notes / tags (e.g. `dropsigma-rma-approved`, `refund-issued-2026-05`) so the merchant can filter Shopify-side reports by RMA state.
- Update shipping addresses or other order metadata when a customer-service correction is made inside a Drop Sigma email thread (rare, but supported).

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

### `read_customers`

**Why we need it.** Drop Sigma's core value is automatically connecting an incoming customer email to the right person and order. To do that we need to read Shopify customer records — primarily to match the inbound email address against an existing customer, and then surface that customer's history inside the AI agent's context window so its drafted reply is informed by what the customer has actually bought from the merchant.

**Specific features that depend on it:**

- Match an incoming customer email to an existing Shopify customer record by email address (so the support thread is linked to the right person, not a stranger).
- Surface a customer's order history (count, lifetime spend, most recent order) inside the inbox sidebar so the agent knows whether they're talking to a first-time buyer or a VIP.
- Feed the AI email-drafting agent the customer's name, locale, and recent orders so replies are personalized rather than generic.
- Power vendor / store reporting on customer cohorts (repeat-purchase rate, etc.).

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

### `write_customers`

**Why we need it.** Reserved for an upcoming feature that writes customer-level tags back to Shopify (e.g. `dropsigma-high-priority`, `dropsigma-refund-issued`) so the merchant can see Drop Sigma's customer-intelligence signals natively inside the Shopify admin without leaving the Customers screen.

**Specific features that depend on it:**

- Planned: tag customers as `high-priority` / `refund-issued` / `at-risk-of-churn` so the merchant can segment in Shopify-native flows / Shopify Email campaigns.
- Planned: write back resolved contact preferences (locale, communication-channel opt-outs) captured during a Drop Sigma support conversation.

**Note for reviewers.** This scope is **not** currently exercised by any production code path. We are listing it preemptively because the feature is on our near-term roadmap; if Shopify prefers a tighter initial surface area, we are happy to drop this scope at submission time and re-request it via an app update once the labeling feature ships.

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

### `read_products`

**Why we need it.** RMA forms, email templates, and Drop Sigma's vendor-routing engine all need to know which products are involved in an order — by name, by image, and by product ID. Without `read_products` we can't render a recognizable "Return for: [Product Name]" header in an RMA, and we can't map products to vendors for our auto-assignment feature.

**Specific features that depend on it:**

- Display product names and thumbnail images inside RMA request forms so customers and merchants can confirm they're returning the right item.
- Populate email templates with the actual product name (e.g. "Your return for the **Wireless Earbuds Pro** has been approved") rather than a bare SKU.
- Power the Permanent Product-Vendor Assignment feature — the merchant maps Shopify product IDs to vendors, and Drop Sigma auto-routes new orders for those products to the right vendor.
- Surface product info inside the AI agent's context when a customer asks about a specific item.

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

### `read_fulfillments`

**Why we need it.** Customer questions overwhelmingly center on "Where is my order?" — Drop Sigma cannot answer that question without reading the fulfillment record (tracking number, carrier, fulfillment status) attached to the order. This scope powers our customer-facing tracking emails and the live-tracking timeline rendered inside the RMA / support thread.

**Specific features that depend on it:**

- Pull tracking numbers and courier names from Shopify fulfillment records and render them in customer-facing "Your order has shipped" / "Out for delivery" emails sent through Drop Sigma.
- Surface the live fulfillment timeline (label printed → in transit → out for delivery → delivered) inside the inbox thread and RMA timeline so support agents can answer "where is it?" without leaving Drop Sigma.
- Feed the AI agent fulfillment state so it can draft accurate "your order shipped on X with courier Y, tracking number Z" replies.
- Drive the Live Tracking Sync feature (we poll the courier site for live status updates).

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

### `write_fulfillments`

**Why we need it.** Drop Sigma's vendor portal lets vendors submit tracking numbers when they ship out the merchant's orders. Those tracking numbers must be pushed back to Shopify so the customer sees the same tracking info inside their Shopify order-confirmation email and Shopify order-status page as they do in the Drop Sigma tracking email — otherwise we create a fragmented customer experience where Drop Sigma "knows" the tracking but Shopify doesn't.

**Specific features that depend on it:**

- After a vendor submits tracking through Drop Sigma's vendor portal and the merchant approves it via the Tracking Approval Queue, Drop Sigma creates / updates the Shopify fulfillment so the tracking is visible in the customer's Shopify order page.
- Mark fulfillments as delivered / closed once our live-tracking engine confirms delivery, keeping Shopify's view in sync.
- Cancel a fulfillment when a vendor / merchant cancels a shipment from inside Drop Sigma.

**Data handling.** All scope data accessed via this permission is stored in our PostgreSQL DB on Railway (US region), accessed only by the connected merchant's authorized users, and purged within 30 days of app uninstallation per Shopify GDPR `shop/redact` policy.

---

## Submission recommendation

**Drop the `write_customers` scope before submission unless we wire the upcoming customer-labeling feature first.** It is not currently used by any production code path, and Shopify reviewers routinely flag unused scopes as a reason to reject or downgrade an app's risk score. We can re-request `write_customers` via a standard app update once the tag-writing feature is live in production — that is a far smoother review path than defending an unused scope in the initial submission.

All other scopes (`read_orders`, `write_orders`, `read_customers`, `read_products`, `read_fulfillments`, `write_fulfillments`) are actively used by shipped features and should remain in the initial submission.
