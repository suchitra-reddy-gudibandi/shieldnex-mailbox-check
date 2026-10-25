# ShieldNex Mailbox Check

A small web app that takes a pasted list of email addresses and marks each one **delivered**, **bounce** or **unknown**.

## How it works
Each address goes through four checks, in order:
1. **Format.** One `@`, a valid local part, a valid domain (no double dots, bad hyphens, etc.).
2. **Domain.** The domain must exist in DNS (NXDOMAIN → bounce).
3. **Mail server.** MX lookup, falling back to A/AAAA. A null MX (`.`) means the domain refuses email → bounce.
4. **Mailbox.** We open an SMTP session to the MX host and send `EHLO`, `MAIL FROM`, `RCPT TO`, then `QUIT`. **`DATA` is never sent, so no message is delivered to anyone.** A made-up address on the same domain is also tested; if the server accepts that too, the domain is a catch-all and the result is *unknown*.

| Result | Meaning |
|---|---|
| delivered | Server accepted the recipient and the domain is not catch-all |
| bounce | Bad format, domain doesn't exist or refuses mail, or server says the mailbox doesn't exist |
| unknown | Server timed out, deferred (greylisting), blocked us, or accepts everything. Also used when step 4 can't run |

## What the deployed version can and cannot check
Many free hosts block outbound port 25. At startup the app tests whether it can reach a mail server. If it can't, the page shows "Mailbox checks are off" and steps 1–3 still run. Addresses on working domains are then **unknown**, never delivered, because the mailbox itself wasn't confirmed. Run it on your own machine or a VPS to get step 4.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py        # http://127.0.0.1:5000
```

## Deploy (Render, free)
Push this folder to GitHub, then Render → New → Web Service → pick the repo. Build: `pip install -r requirements.txt`. Start: `gunicorn app:app --workers 1 --threads 8 --timeout 120`. Render serves it over HTTPS automatically, and the app redirects any HTTP request to HTTPS.

## Settings (optional environment variables)
`SMTP_PROBE` = `auto` (default), `on` or `off` · `MAX_ADDRESSES` (default 100) · `HELO_HOST` · `PROBE_FROM` (default: null sender)

## Files
`app.py` (checks + API) · `templates/index.html` · `static/style.css` · `static/app.js` · `static/logo.svg` · `requirements.txt` · `render.yaml`
