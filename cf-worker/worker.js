/**
 * Drop Sigma WooCommerce relay — runs on Cloudflare Workers free tier.
 *
 * Why: Managed WordPress hosts (Kinsta, WP Engine, SiteGround,
 *      Hostinger, Cloudways) block API requests from datacenter IPs
 *      at their edge firewall. Drop Sigma runs on Railway, so it
 *      gets 403'd. BUT Cloudflare's IPs are universally trusted —
 *      Kinsta literally uses Cloudflare as a CDN partner — so a
 *      request originating from Cloudflare's network gets through.
 *
 * What this worker does:
 *   • Receives an HTTPS request from Drop Sigma (any method).
 *   • Reads the target URL from the `X-Target-URL` header.
 *   • Authenticates via shared secret in `X-Proxy-Secret` header.
 *   • Forwards the request to the target with all original headers
 *     (Authorization: Basic …, Accept: json, etc) intact.
 *   • Returns the upstream response (status, headers, body)
 *     verbatim.
 *
 * Free-tier limits:
 *   • 100,000 requests / day (Drop Sigma uses < 1,000 / day per store)
 *   • 10ms CPU time per request (we use < 1ms — just header juggling)
 *   • 1 worker per free account
 *
 * Setup (5 min, one-time):
 *   1. Sign up at https://dash.cloudflare.com/sign-up — no card required
 *   2. Workers & Pages → Create → Worker → name it (e.g. "dropsigma-relay")
 *   3. Edit code → paste this file → Save → Deploy
 *   4. Settings → Variables → Add `PROXY_SECRET` (generate a random
 *      32-char string; you'll set this in Drop Sigma's env too)
 *   5. Copy the worker URL (e.g. https://dropsigma-relay.YOUR.workers.dev)
 *   6. In Railway / Drop Sigma env, set:
 *         WOO_CF_PROXY_URL    = https://dropsigma-relay.YOUR.workers.dev
 *         WOO_CF_PROXY_SECRET = <same 32-char string as in step 4>
 *
 * That's it. Working stores stay working (worker is never used for
 * them). Kinsta-blocked stores get auto-routed through this worker
 * → Kinsta accepts the Cloudflare-IP request → store flips Online.
 */

export default {
  async fetch(request, env) {
    // ── 1. Auth — only Drop Sigma can use this proxy ────────────
    const providedSecret = request.headers.get('X-Proxy-Secret') || '';
    const expectedSecret = env.PROXY_SECRET || '';
    if (!expectedSecret || providedSecret !== expectedSecret) {
      return new Response('Unauthorized — invalid or missing X-Proxy-Secret', {
        status: 401,
        headers: { 'Content-Type': 'text/plain' },
      });
    }

    // ── 2. Target URL must be supplied ───────────────────────────
    const targetUrl = request.headers.get('X-Target-URL') || '';
    if (!targetUrl || !/^https?:\/\//i.test(targetUrl)) {
      return new Response('Bad Request — missing or invalid X-Target-URL', {
        status: 400,
        headers: { 'Content-Type': 'text/plain' },
      });
    }

    // ── 3. Build forwarded request ──────────────────────────────
    // Strip our own protocol headers; pass everything else through
    // so the upstream sees Drop Sigma's real Authorization + Accept
    // + User-Agent + auth params + body bytes etc.
    const fwdHeaders = new Headers();
    for (const [name, value] of request.headers.entries()) {
      const lower = name.toLowerCase();
      if (lower === 'x-proxy-secret') continue;
      if (lower === 'x-target-url')   continue;
      if (lower === 'host')           continue;  // CF rewrites it
      if (lower === 'cf-connecting-ip') continue;
      if (lower === 'cf-ipcountry')   continue;
      if (lower === 'cf-ray')         continue;
      if (lower === 'cf-visitor')     continue;
      if (lower === 'cf-worker')      continue;
      fwdHeaders.set(name, value);
    }

    let body = null;
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      body = await request.arrayBuffer();
    }

    // ── 4. Forward + return ─────────────────────────────────────
    try {
      const upstream = await fetch(targetUrl, {
        method:   request.method,
        headers:  fwdHeaders,
        body:     body,
        redirect: 'follow',
        // Cloudflare-specific: don't cache, don't add CF's own
        // optimisations (they'd alter the response shape).
        cf: { cacheTtl: 0, cacheEverything: false },
      });

      // Pass the upstream response through verbatim. Strip CF's
      // injected response headers so Drop Sigma doesn't see them.
      const respHeaders = new Headers();
      for (const [name, value] of upstream.headers.entries()) {
        const lower = name.toLowerCase();
        if (lower.startsWith('cf-')) continue;
        respHeaders.set(name, value);
      }
      respHeaders.set('X-Proxy-Via', 'cloudflare-workers');

      return new Response(upstream.body, {
        status:     upstream.status,
        statusText: upstream.statusText,
        headers:    respHeaders,
      });
    } catch (err) {
      return new Response(
        `Worker fetch failed: ${err.message}`,
        { status: 502, headers: { 'Content-Type': 'text/plain' } },
      );
    }
  },
};
