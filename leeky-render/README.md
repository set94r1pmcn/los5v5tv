# Leeky — Render deployment

## Deploy

1. Create a private GitHub repository. Upload the CONTENTS of this folder at its root, including Dockerfile and render.yaml. Do not upload the ZIP itself to the repository.
2. In Render, choose New > Blueprint and connect that repository. The blueprint requests a paid Starter web service and a 10 GB persistent disk; review Render's current charges before creating it.
3. Enter your Rune Pay MERCHANT key in RUNEPAY_API_KEY when Render asks. It begins rp_sec_. Never put the key in GitHub, browser code, or a chat message. A payout key is unnecessary.
4. Deploy. Render's supplied RENDER_EXTERNAL_URL configures checkout return URLs, callbacks, and secure cookies automatically.
5. For a custom domain, add it in Render and set PUBLIC_URL to its full HTTPS origin, with no trailing slash, then redeploy.

The server listens on Render's PORT on 0.0.0.0. Docker installs FFmpeg. /healthz is the health check.

## Videos and saved access

Public demo sources go in dist/videos/ before deploying. The supplied site currently has no bundled video files. Upload paid video sources to /var/data/private-videos/ through Render's shell/SSH or a secure file transfer appropriate to your account; the site has no admin upload screen. Do not place paid videos in dist or GitHub.

SQLite orders, access keys, sessions, thumbnail caches, and watermarked copies live under /var/data/access/. Back up the whole persistent disk securely. Keep one service instance: this implementation uses a local SQLite database and local files. Size the disk for originals plus processed video copies. First playback can take time while watermarking runs; long videos may need preprocessing and a larger service.

## Payments

The existing integration uses https://rpay.gg/api/v1/payment/white-label, merchant_api_key authentication, signed callbacks at /api/rpay/callback, and payment reconciliation at GET /payment/{track_id}. Price remains $14.99 lifetime access. No live invoice or charge was created during preparation.

Only confirmed Paid records matching the stored order, track ID, amount and currency grant access. White-label payments have no sandbox mode; anything marked sandbox=true is refused. Signed callback bodies are verified with HMAC-SHA512 using the SHA-256 hexadecimal digest of the merchant key. Repeated callbacks return the same access key. Ambiguous invoice failures block automatic retry to avoid duplicate invoices.

Run one real end-to-end checkout after deploying (pay the $14.99 yourself) and confirm the access key unlocks the library before announcing the site.

Reference: https://rpay.gg/docs
Render configuration: https://render.com/docs/blueprint-spec

## Verification

Local payment validation and direct request-handler checks passed for checkout payloads, invoice reuse, secure cookies, forged signature rejection, sandbox rejection, matching signed callbacks, duplicate fulfilment, login, and receipt lookup. Python compilation passed. Local HTTP tests were blocked by this environment's socket restrictions. Docker build, deployed HTTPS behavior, FFmpeg playback, and live merchant API calls remain unverified.

Full HTTP suite where local servers are permitted: python test_access.py
Local preview: python server.py --port 8767

## Embedded crypto checkout update

Checkout now loads active coins/networks from Rune Pay reference endpoints, creates a white-label invoice, and displays the provider-issued QR code, exact crypto amount, address, required memo/tag, and expiry. The provider key stays on the server. Receipt cookies restore an unfinished invoice; repeated checkout requests reuse it. The page checks local state every 10 seconds while open; remote reconciliation is limited to once every 120 seconds, with signed callbacks providing updates. Confirming, underpaid, expired, cancelled, and failed statuses are displayed. Access is released only after verified live Paid details match.

Set RUNEPAY_API_KEY in Render and deploy before using payments. No merchant credentials were available during development, so authenticated API calls and QR retrieval have not been verified against a live invoice. The separate payment-preview.html uses clearly labelled design fixtures and a QR encoding “DESIGN PREVIEW ONLY - NOT A PAYMENT ADDRESS”. Those fixtures are absent from the deployable site.

Offline request-handler test: python test_payment_flow.py

## October 2026 fixes

- Rate limits now use the visitor's real IP from X-Forwarded-For (Render's proxy IP was being shared by everyone).
- Removed the public thumbnail upload endpoint; the server generates all thumbnails itself.
- Payment reconciliation no longer requires order_id in Rune Pay's GET /payment/{track_id} response; the stored order for that track is used. sandbox=false is still required.
- Views and likes are real, stored in SQLite (one view per visitor per video every 6 hours). The checkout shows the real number of paid clips. Preview timelines show true durations. Library search filters clip titles.
- Redesigned crypto checkout popup.
- Clips can be kept in one level of subfolders (e.g. /var/data/private-videos/Ranked/clutch.mp4); they all show together in the library.

Tests: python test_payment_flow.py && python test_fixes.py && python test_access.py


The [ view more ] button shows at the bottom of the library for visitors without access, whenever the paid library has clips. It returns to the landing page with the Access button focused. Library search is for members only.

## Rune Pay check (October 23, 2026)

Checked against https://rpay.gg/docs and the live public endpoints /common/currencies and /common/status:
- White-label request now sends only documented fields, with amount as a number. The undocumented sandbox and return_url fields were removed.
- Rune Pay's payment details and callbacks do not include a sandbox field. The old code required sandbox=false and would never have unlocked a sale; it now refuses only payments explicitly marked as sandbox.
- QR or checkout links from any rpay.gg subdomain are accepted; an unexpected QR link falls back to the copyable address instead of failing checkout.
- A rejected invoice request (4xx) is marked Failed so the buyer can retry; network or server errors still lock that receipt for review to avoid double invoices.
