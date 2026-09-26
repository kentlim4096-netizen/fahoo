"""QR-code -> TOTP secret helpers and a code generator (shared by the setup wizard and the code window).

  qr_from_file(path)     decode a QR from an image file (screenshot / photo)
  qr_from_screen()       take a screenshot of all monitors and look for a QR in it
  parse_secret(text)     accept an otpauth:// URI, a Base32 secret (spaces/dashes ok) -> TotpSecret
  code_now(secret)       the current TOTP code, seconds left in this 30 s window

Secrets are never printed or logged by this module.
"""
import base64, hashlib, hmac, os, re, struct, time, urllib.parse
from dataclasses import dataclass


@dataclass
class TotpSecret:
    secret: str                 # Base32, upper-case, no padding/spaces
    issuer: str = ""
    account: str = ""
    digits: int = 6
    period: int = 30
    algorithm: str = "sha1"

    @property
    def is_default_kind(self):
        """The service's own TOTP generator is SHA1 / 6 digits / 30 s (KR883's kind)."""
        return self.algorithm == "sha1" and self.digits == 6 and self.period == 30


def _b32(s):
    s = re.sub(r"[\s-]", "", s or "").upper().rstrip("=")
    base64.b32decode(s + "=" * ((8 - len(s) % 8) % 8))       # raises binascii.Error if not Base32
    return s


def parse_secret(text):
    """otpauth://totp/Issuer:account?secret=...&issuer=... or a bare Base32 secret."""
    text = (text or "").strip()
    if text.lower().startswith("otpauth://"):
        u = urllib.parse.urlparse(text)
        q = urllib.parse.parse_qs(u.query)
        label = urllib.parse.unquote(u.path.lstrip("/"))
        issuer = (q.get("issuer") or [""])[0]
        account = label
        if ":" in label:
            iss2, account = label.split(":", 1)
            issuer = issuer or iss2
        if "secret" not in q:
            raise ValueError("The QR code has no secret in it.")
        return TotpSecret(_b32(q["secret"][0]), issuer.strip(), account.strip(),
                          int((q.get("digits") or ["6"])[0]), int((q.get("period") or ["30"])[0]),
                          (q.get("algorithm") or ["SHA1"])[0].lower())
    return TotpSecret(_b32(text))


def code_now(ts, at=None):
    """-> (code string, seconds left in the current window)."""
    at = time.time() if at is None else at
    key = base64.b32decode(ts.secret + "=" * ((8 - len(ts.secret) % 8) % 8))
    counter = int(at // ts.period)
    h = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, ts.algorithm, hashlib.sha1)).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** ts.digits)
    return str(code).zfill(ts.digits), ts.period - int(at % ts.period)


def _decode_image(img):
    """All QR payloads found in an OpenCV image. Retries at a few scales - screenshots and phone
    photos often miss on the first pass."""
    import cv2
    det = cv2.QRCodeDetector()
    for scale in (1.0, 2.0, 3.0, 4.0, 0.5):
        im = img if scale == 1.0 else cv2.resize(img, None, fx=scale, fy=scale,
                                                  interpolation=cv2.INTER_NEAREST if scale > 1 else cv2.INTER_AREA)
        try:
            ok, infos, _pts, _ = det.detectAndDecodeMulti(im)
            found = [t for t in (infos if ok else []) if t]
        except Exception:
            found = []
        if not found:
            t, _p, _s = det.detectAndDecode(im)
            found = [t] if t else []
        if found:
            return found
        gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) if im.ndim == 3 else im
        t, _p, _s = det.detectAndDecode(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
        if t:
            return [t]
    return []


def _first_totp(payloads):
    for p in payloads:
        if p.lower().startswith("otpauth://"):
            return parse_secret(p)
    if payloads:
        raise ValueError("A QR code was found, but it is not an authenticator (otpauth://) code.")
    return None


def qr_from_file(path):
    """TotpSecret from an image file, or None if no QR was found."""
    import cv2, numpy as np
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)     # works with non-ASCII paths
    if img is None:
        raise ValueError("That file is not an image I can read.")
    return _first_totp(_decode_image(img))


def qr_from_screen():
    """Screenshot every monitor and look for an authenticator QR on screen."""
    import cv2, numpy as np
    from PIL import ImageGrab
    shot = ImageGrab.grab(all_screens=True).convert("RGB")
    img = cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)
    return _first_totp(_decode_image(img))
