"""ShieldNex Mailbox Check: email deliverability checker (delivered / bounce / unknown)."""
import os, random, re, smtplib, socket, string, threading, time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from difflib import get_close_matches

import dns.exception
import dns.resolver
from flask import Flask, jsonify, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

MAX_ADDRESSES = int(os.environ.get("MAX_ADDRESSES", 100))
MAX_PER_REQUEST = 10
SMTP_MODE = os.environ.get("SMTP_PROBE", "auto").lower()   # auto | on | off
SMTP_TIMEOUT = 6
HELO_HOST = os.environ.get("HELO_HOST", socket.getfqdn() or "localhost")
PROBE_FROM = os.environ.get("PROBE_FROM", "")               # empty = null sender <>
COMMON = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
          "live.com", "proton.me", "rediffmail.com", "yahoo.co.in"]

resolver = dns.resolver.Resolver()
resolver.lifetime = resolver.timeout = 5


class TTLCache:
    def __init__(self, ttl=600):
        self.ttl, self.data, self.lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            hit = self.data.get(key)
            return hit[1] if hit and hit[0] > time.time() else None

    def set(self, key, value):
        with self.lock:
            self.data[key] = (time.time() + self.ttl, value)


dns_cache, catch_cache, port_cache = TTLCache(), TTLCache(), TTLCache()
_locks, _guard = defaultdict(threading.Lock), threading.Lock()


def domain_lock(domain):  # one probe at a time per domain, so we never hammer a server
    with _guard:
        return _locks[domain]


# ---------- 1. format ----------
LOCAL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


def check_format(address):
    if address.count("@") != 1:
        return None, "An address needs exactly one @ sign."
    local, domain = address.split("@")
    if not local:
        return None, "There is nothing before the @ sign."
    if not domain:
        return None, "There is nothing after the @ sign."
    if len(local) > 64:
        return None, "The part before @ is longer than 64 characters."
    if not LOCAL_RE.match(local):
        return None, "The part before @ has a double dot, a leading or trailing dot, or a character mail servers reject."
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None, "The domain name isn't valid."
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253:
        return None, "The domain needs a dot, like example.com."
    if not all(LABEL_RE.match(l) for l in labels):
        return None, "The domain contains invalid characters or misplaced hyphens."
    if not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}", labels[-1]):
        return None, "The domain ending isn't valid."
    return (local, domain), None


# ---------- 2-3. domain + mail server ----------
def lookup_domain(domain):
    cached = dns_cache.get(domain)
    if cached:
        return cached
    try:
        try:
            answers = sorted(resolver.resolve(domain, "MX"), key=lambda r: r.preference)
            hosts = [str(r.exchange).rstrip(".") for r in answers]
            info = {"state": "nomail", "hosts": []} if hosts == [""] else {"state": "ok", "hosts": hosts}
        except dns.resolver.NoAnswer:  # no MX: RFC 5321 falls back to the A/AAAA record
            for rtype in ("A", "AAAA"):
                try:
                    resolver.resolve(domain, rtype)
                    info = {"state": "ok", "hosts": [domain]}
                    break
                except dns.resolver.NoAnswer:
                    continue
            else:
                info = {"state": "nomail", "hosts": []}
    except dns.resolver.NXDOMAIN:
        info = {"state": "nxdomain", "hosts": []}
    except (dns.exception.DNSException, OSError):
        return {"state": "error", "hosts": []}  # not cached: may be a temporary failure
    dns_cache.set(domain, info)
    return info


# ---------- 4. mailbox (SMTP RCPT TO only, never DATA) ----------
def smtp_available():
    if SMTP_MODE in ("on", "off"):
        return SMTP_MODE == "on"
    cached = port_cache.get("p25")
    if cached is not None:
        return cached
    ok = False
    for host in ("gmail-smtp-in.l.google.com", "alt1.gmail-smtp-in.l.google.com"):
        try:
            with socket.create_connection((host, 25), timeout=4) as s:
                s.settimeout(4)
                if s.recv(64).startswith(b"220"):
                    ok = True
                    break
        except OSError:
            continue
    port_cache.set("p25", ok)
    return ok


REJECT_RE = re.compile(r"5\.1\.[1-6]|user unknown|unknown user|no such (user|mailbox|recipient)|does not exist|"
                       r"doesn't exist|mailbox (unavailable|not found|disabled)|invalid (recipient|mailbox|address)|"
                       r"recipient (not found|rejected)|account (disabled|not found)", re.I)


def smtp_rcpt(host, address):
    """Ask one mail server whether it would accept `address`. Returns (outcome, reply)."""
    try:
        smtp = smtplib.SMTP(host, 25, local_hostname=HELO_HOST, timeout=SMTP_TIMEOUT)
    except smtplib.SMTPConnectError as e:
        return "blocked", str(e)[:120]
    except (OSError, smtplib.SMTPException) as e:
        return "unreachable", type(e).__name__
    try:
        smtp.ehlo_or_helo_if_needed()
        code, msg = smtp.mail(PROBE_FROM)
        if code != 250:
            return "blocked", f"{code} sender refused"
        code, msg = smtp.rcpt(address)
        reply = f"{code} {msg.decode('utf-8', 'replace')}"[:140]
        if code in (250, 251):
            return "accept", reply
        if 400 <= code < 500:
            return "defer", reply
        return ("reject" if REJECT_RE.search(reply) else "blocked"), reply
    except (OSError, smtplib.SMTPException) as e:
        return "unreachable", type(e).__name__
    finally:
        try:
            smtp.quit()
        except Exception:
            smtp.close()


def verify_mailbox(domain, hosts, address):
    with domain_lock(domain):
        outcome, reply, used = "unreachable", "", hosts[0]
        for used in hosts[:2]:
            outcome, reply = smtp_rcpt(used, address)
            if outcome != "unreachable":
                break
        if outcome != "accept":
            return outcome, reply, False
        catch_all = catch_cache.get(domain)
        if catch_all is None:  # does the domain accept a made-up address too?
            fake = "zz" + "".join(random.choices(string.ascii_lowercase + string.digits, k=14)) + "@" + domain
            catch_all = smtp_rcpt(used, fake)[0] == "accept"
            catch_cache.set(domain, catch_all)
        return "accept", reply, catch_all


def check_one(raw):
    address = str(raw).strip()
    res = {"email": address, "status": "unknown", "reason": "", "suggestion": None}
    try:
        parsed, problem = check_format(address)
        if problem:
            return {**res, "status": "bounce", "reason": problem}
        local, domain = parsed
        close = [] if domain in COMMON else get_close_matches(domain, COMMON, n=1, cutoff=0.85)
        res["suggestion"] = f"{local}@{close[0]}" if close else None
        info = lookup_domain(domain)
        if info["state"] == "nxdomain":
            return {**res, "status": "bounce", "reason": f"The domain {domain} doesn't exist."}
        if info["state"] == "nomail":
            return {**res, "status": "bounce", "reason": f"{domain} doesn't accept email."}
        if info["state"] == "error":
            return {**res, "reason": "The DNS lookup failed or timed out. Try again."}
        if not smtp_available():
            return {**res, "reason": f"{domain} accepts email, but this host can't confirm individual mailboxes."}
        outcome, reply, catch_all = verify_mailbox(domain, info["hosts"], address)
        if outcome == "accept" and not catch_all:
            return {**res, "status": "delivered", "reason": "The mail server confirmed this mailbox exists."}
        if outcome == "accept":
            return {**res, "reason": f"{domain} accepts mail for any address, so this mailbox can't be confirmed."}
        if outcome == "reject":
            return {**res, "status": "bounce", "reason": "The mail server says this mailbox doesn't exist."}
        if outcome == "defer":
            return {**res, "reason": "The server asked us to try later (common with greylisting)."}
        if outcome == "blocked":
            return {**res, "reason": "The server wouldn't say whether this mailbox exists."}
        return {**res, "reason": f"Couldn't connect to the mail server for {domain}."}
    except Exception:
        return {**res, "reason": "Something went wrong while checking this address."}


# ---------- web ----------
hits, hits_lock = defaultdict(deque), threading.Lock()


def rate_limited(ip, limit=60):
    now = time.time()
    with hits_lock:
        q = hits[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
        return False


@app.before_request
def force_https():  # Render/PythonAnywhere terminate TLS and tell us the original scheme
    if request.headers.get("X-Forwarded-Proto", "https") == "http":
        return redirect(request.url.replace("http://", "https://", 1), code=301)


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                                               "font-src https://fonts.gstatic.com; img-src 'self' data:")
    if request.is_secure:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    return resp


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/status")
def status():
    return jsonify(mailbox_checks=smtp_available(), max_addresses=MAX_ADDRESSES, chunk=MAX_PER_REQUEST)


@app.post("/api/check")
def check():
    if rate_limited(request.remote_addr):
        return jsonify(error="Too many requests. Wait a minute and try again."), 429
    emails = (request.get_json(silent=True) or {}).get("emails")
    if not isinstance(emails, list) or not emails or len(emails) > MAX_PER_REQUEST:
        return jsonify(error=f"Send between 1 and {MAX_PER_REQUEST} addresses per request."), 400
    with ThreadPoolExecutor(max_workers=len(emails)) as pool:
        return jsonify(results=list(pool.map(check_one, [str(e)[:320] for e in emails])))


threading.Thread(target=smtp_available, daemon=True).start()  # warm the port-25 test

if __name__ == "__main__":
    app.run(debug=True, port=5000)
