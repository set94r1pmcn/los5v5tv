# Deploy Leeky

1. Unzip leeky-render.zip. Upload the contents of leeky-render/ to a private GitHub repository. Dockerfile and render.yaml must be at the repository root.
2. In Render select New → Blueprint. Connect the repository.
3. When asked for RUNEPAY_API_KEY, paste your Rune Pay merchant key (rp_sec_…). Set it only in Render, never in the source files.
4. Review the paid Starter service and 10 GB disk, then deploy.
5. Open the Render URL. Click Access to open the payment popup.

The popup includes your logo, the supplied content labels, the $14.99 price, coin/network selection, the Rune Pay QR code and address, and access-key delivery. It is part of the site; payment-preview.html is a separate non-payable design preview and is excluded from this deployment.

Render sets the HTTPS address automatically. If you connect a custom domain, set PUBLIC_URL to that HTTPS origin (e.g. https://leeky.wtf) without a trailing slash, then redeploy. The callback is https://YOUR-DOMAIN/api/rpay/callback; invoices send this callback address to Rune Pay automatically.

## Add your videos

This package contains no videos. Demo sources go in dist/videos/ before deployment. Paid originals go on the persistent disk at /var/data/private-videos/ using Render's shell/SSH or another secure transfer appropriate to your account. No browser upload tool is included.

Orders, access keys, and processed videos are saved in /var/data/access/. Back up the persistent disk. Keep one service instance. Videos are watermarked on first playback, which may take time for long videos.

## Before taking customer payments

The integration has passed local simulated payment tests. A Docker build, deployed browser flow, FFmpeg playback, authenticated Rune Pay API calls, and live invoice QR have not been verified here.

Rune Pay white-label payments have no sandbox mode, so the first test purchase uses real crypto. Pay $14.99 yourself once after deploying to confirm the whole flow.

Test an entire checkout and callback after deployment, including access-key login and playback. No payment was created or charged while preparing this package.

References: https://rpay.gg/docs and https://render.com/docs/blueprint-spec
