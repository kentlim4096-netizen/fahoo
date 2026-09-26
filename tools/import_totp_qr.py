"""Read a TOTP setup QR (screenshot or photo) and write its secret straight into .env.

    python tools/import_totp_qr.py <image> [--var KW388_TOTP_SECRET] [--dry-run]

The secret is never printed — it goes from the image into .env and nothing else. What IS printed
is the issuer/account the QR names plus the TOTP code valid right now, so you can check it against
your authenticator app and know the import worked before running a scrape.
"""
import argparse, base64, os, re, sys, urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

ENV_PATH = os.path.join(HERE, ".env")


def decode_qr(path):
    """Return every payload string found in the image. Tries the plain decode first, then a few
    upscales — phone photos and small screenshots often miss on the first pass."""
    try:
        import cv2
    except ImportError:
        sys.exit("opencv is required: pip install opencv-python-headless")
    import numpy as np

    data = np.fromfile(path, dtype=np.uint8)  # handles non-ASCII paths, unlike cv2.imread
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        sys.exit(f"Could not read an image from {path}")

    det = cv2.QRCodeDetector()
    attempts = [img]
    for scale in (2, 3, 4):
        attempts.append(cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    attempts.append(cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])

    for candidate in attempts:
        try:
            ok, payloads, _pts, _ = det.detectAndDecodeMulti(candidate)
            if ok:
                found = [p for p in payloads if p]
                if found:
                    return found
        except cv2.error:
            pass
        try:
            single, _pts, _ = det.detectAndDecode(candidate)
            if single:
                return [single]
        except cv2.error:
            pass
    return []


def parse_payload(payload):
    """otpauth://totp/Issuer:account?secret=...&issuer=...  -> dict. Also accepts a bare secret."""
    info = {"issuer": "", "account": "", "digits": 6, "period": 30, "algorithm": "sha1"}
    if payload.lower().startswith("otpauth://"):
        u = urllib.parse.urlparse(payload)
        q = urllib.parse.parse_qs(u.query)
        secret = (q.get("secret") or [""])[0]
        label = urllib.parse.unquote(u.path.lstrip("/"))
        if ":" in label:
            info["issuer"], info["account"] = label.split(":", 1)
        else:
            info["account"] = label
        if q.get("issuer"):
            info["issuer"] = q["issuer"][0]
        info["digits"] = int((q.get("digits") or [6])[0])
        info["period"] = int((q.get("period") or [30])[0])
        info["algorithm"] = (q.get("algorithm") or ["sha1"])[0].lower()
        if u.netloc.lower() not in ("totp", ""):
            print(f"  ! QR is {u.netloc.upper()}, not TOTP — this scraper only handles TOTP")
    else:
        secret = payload.strip().replace(" ", "")
    info["secret"] = secret.strip().replace(" ", "").upper()
    return info


def validate(secret):
    if not secret:
        return "no secret found in the QR"
    if not re.fullmatch(r"[A-Z2-7]+=*", secret):
        return "secret is not valid base32"
    try:
        base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    except Exception as e:
        return f"secret failed base32 decode: {e}"
    return None


def upsert_env(var, value):
    """Set var=value in .env, replacing any existing line for it and preserving everything else."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    out, replaced = [], False
    for line in lines:
        if re.match(rf"\s*{re.escape(var)}\s*=", line):
            if not replaced:
                out.append(f"{var}={value}")
                replaced = True
            continue  # drop any duplicate definitions
        out.append(line)
    if not replaced:
        out.append(f"{var}={value}")
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, ENV_PATH)
    return replaced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--var", default="KW388_TOTP_SECRET")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    payloads = decode_qr(a.image)
    if not payloads:
        sys.exit("No QR code could be read from that image. Try a larger/sharper crop of just the code.")

    otp = [p for p in payloads if p.lower().startswith("otpauth://")]
    info = parse_payload(otp[0] if otp else payloads[0])

    err = validate(info["secret"])
    if err:
        sys.exit(f"That QR doesn't hold a usable TOTP secret — {err}")

    print(f"  issuer   : {info['issuer'] or '(none)'}")
    print(f"  account  : {info['account'] or '(none)'}")
    print(f"  secret   : {len(info['secret'])} base32 chars (not shown)")
    print(f"  algorithm: {info['algorithm']}, {info['digits']} digits, {info['period']}s period")

    if info["algorithm"] != "sha1" or info["digits"] != 6 or info["period"] != 30:
        print("  ! Non-default TOTP parameters — scraper_service.totp() assumes sha1/6/30.")

    from scraper_service import totp
    print(f"\n  code right now: {totp(info['secret'], digits=info['digits'], period=info['period'], algo=info['algorithm'])}")
    print("  ^ check this matches your authenticator app before running a scrape.\n")

    if a.dry_run:
        print("  --dry-run: .env not modified")
        return
    replaced = upsert_env(a.var, info["secret"])
    print(f"  {'Updated' if replaced else 'Added'} {a.var} in .env")


if __name__ == "__main__":
    main()
