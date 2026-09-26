"""Credit Report access audit log (logging only - no effect on the lookup workflow).

One line per lookup through /credit-report and /check, in data/logs/credit_report_access.log
(size-rotated), so an unknown lookup can be traced: WHO -> WHICH endpoint -> WHEN -> FROM WHERE ->
WHICH UI stages ran (via lookup_id in the provider's stage log) -> RESULT.

    2026-09-27 04:30:15.382 | /credit-report | worker | ***5533 | 127.0.0.1 | LOCAL | NOT_MEMBER | 0.7s | lookup_id=1a2b3c4d5e6f | stage=-

NEVER written: the full IC (last 4 digits only), passwords, OTP/TOTP, tokens, cookies, the
Authorization header, session contents. Forwarded-for headers are recorded only as sanitised,
informational text - they are client-controlled and are never used for any security decision.
"""
import datetime, ipaddress, logging, logging.handlers, os, re, uuid

_ALLOWED_FWD = re.compile(r"[^0-9A-Fa-f:.,\[\] ]")          # an IP list only - strips anything else
_FWD_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Proto", "X-Forwarded-Host")


def new_lookup_id():
    return uuid.uuid4().hex[:12]


def mask_ic(ic):
    d = re.sub(r"\D", "", str(ic or ""))
    return "***" + d[-4:] if len(d) >= 4 else "***"


def setup(path, max_bytes=2_000_000, backups=5):
    """Dedicated rotating logger. Rotation caps total disk use at about max_bytes * (backups + 1)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lg = logging.getLogger("credit_report_access")
    lg.setLevel(logging.INFO)
    lg.propagate = False                                     # never leaks into the service's stderr
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers):
        h = logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
    return lg


def source_info(request):
    """-> (direct_peer_ip, 'LOCAL'|'PROXIED'|'REMOTE', sanitised_forwarded_or_empty).

    The direct peer is the TCP peer of the connection. NOTE: traffic that arrives through the ngrok
    tunnel also has a loopback peer (the ngrok agent runs on this machine), so a loopback peer does
    NOT by itself mean the person is local - forwarding headers mark such requests PROXIED."""
    peer = ""
    try:
        peername = request.transport.get_extra_info("peername") if request.transport else None
        peer = peername[0] if peername else ""
    except Exception:
        pass
    fwd_present = any(request.headers.get(h) for h in _FWD_HEADERS)
    fwd = _ALLOWED_FWD.sub("", request.headers.get("X-Forwarded-For", "") or request.headers.get("X-Real-IP", ""))[:100].strip()
    if fwd_present:
        kind = "PROXIED"
    else:
        try:
            ip = ipaddress.ip_address(peer)
            kind = "LOCAL" if (ip.is_loopback or ip.is_private or ip.is_link_local) else "REMOTE"
        except ValueError:
            kind = "REMOTE"
    return _ALLOWED_FWD.sub("", peer) or "?", kind, fwd


def format_record(endpoint, role, ic, peer, kind, result, duration_s, lookup_id, stage=None, fwd="", when=None):
    when = when or datetime.datetime.now()
    ts = when.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (when.microsecond // 1000)
    line = (f"{ts} | {endpoint} | {re.sub(r'[^a-z]', '', str(role).lower()) or '?'} | {mask_ic(ic)} | {peer} | {kind} | "
            f"{result} | {duration_s:.1f}s | lookup_id={lookup_id} | stage={stage or '-'}")
    if fwd:
        line += f" | fwd={fwd}"
    return line


def write(logger, request, endpoint, role, ic, result, duration_s, lookup_id, stage=None):
    """Never raises - auditing must not be able to break a lookup."""
    try:
        peer, kind, fwd = source_info(request)
        logger.info(format_record(endpoint, role, ic, peer, kind, result, duration_s, lookup_id, stage, fwd))
    except Exception:
        pass
