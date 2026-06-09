# Cloudflare Worker setup — free fix for Kinsta/WAF-blocked WC stores

**Time required:** 5 minutes, one-time.
**Cost:** $0. Cloudflare's free tier covers 100,000 worker requests
per day. Drop Sigma uses <1,000 requests/day per WAF-blocked store,
so even with dozens of such stores you'd stay free forever.

## Why this exists

Managed WordPress hosts (Kinsta, WP Engine, SiteGround, Cloudways,
Hostinger) block API requests from datacenter IPs at the edge. Drop
Sigma runs on Railway → its outbound IP is a datacenter IP → blocked.

Cloudflare's IPs are NOT in those blocklists — most managed hosts
WHITELIST Cloudflare because they use it themselves. So if we route
the WC API call through a Cloudflare Worker, the host sees a
Cloudflare IP and lets it through.

Drop Sigma's WC retry tier (in `orders/services.py::_curl_cffi_retry`)
already supports this — it just needs two env vars to know where to
relay through.

## Step-by-step

### 1. Sign up for Cloudflare (skip if you have an account)

- Go to https://dash.cloudflare.com/sign-up
- Email + password — NO credit card required
- Verify email

### 2. Create a worker

- In the dashboard left nav: **Workers & Pages** → **Create**
- Pick **Create Worker** (NOT Pages)
- Name it anything — suggested: `dropsigma-relay`
- Click **Deploy** to create the default hello-world worker
- After deploy, click **Edit code**

### 3. Paste the worker code

- Delete everything in the editor
- Open `cf-worker/worker.js` from this repo
- Copy its entire contents → paste into the Cloudflare editor
- Click **Save and deploy**

### 4. Set the shared secret

This prevents random people from using your worker as an open proxy.

- Generate a random 32+ char string:
  ```bash
  openssl rand -hex 32
  ```
  (or any password manager — just keep it secret)
- In the worker's page: **Settings** → **Variables** → **Environment Variables**
- Click **Add variable**:
  - Variable name: `PROXY_SECRET`
  - Value: paste the random string
  - Check **Encrypt** so it's not visible in the UI later
- Click **Save and deploy**

### 5. Copy the worker URL

At the top of the worker page you'll see something like:
```
https://dropsigma-relay.YOUR-NAME.workers.dev
```
Copy this URL.

### 6. Add the env vars to Drop Sigma (Railway)

In the Railway dashboard → Drop Sigma service → **Variables**:

```
WOO_CF_PROXY_URL    = https://dropsigma-relay.YOUR-NAME.workers.dev
WOO_CF_PROXY_SECRET = <the same random string from step 4>
```

Save. Railway auto-redeploys.

### 7. Verify

After Railway redeploys (~2 min):

1. Open `/dashboard/?section=stores`
2. Click **Reconnect** on the previously-blocked store
3. Expected: store flips **Online**

In Railway logs, you should see:
```
WooCommerce WAF retry chrome+cfworker succeeded for https://… (HTTP 200)
```

## What happens for normal stores

Nothing. The worker is ONLY used inside the WAF-handshake retry tier,
which only fires when the primary cloudscraper request raises an SSL
handshake exception. Working stores never hit the retry tier, so the
worker is never invoked for them. Zero impact, zero cost.

## Updating the worker code

If we improve `worker.js` later, just paste the new version into the
Cloudflare editor and click **Save and deploy**. Takes 10 seconds.

## Tearing down

Worker not needed anymore? In Cloudflare → Workers & Pages → click
the worker → **Manage** → **Delete**. In Railway, remove the two env
vars. Drop Sigma falls back to the firewall-card UX automatically.
