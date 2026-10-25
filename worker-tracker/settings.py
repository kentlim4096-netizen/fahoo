"""Tracker settings (data/settings.json): which alerts are on, schedule, targets, and the user's own
classification rules for KR883 save/update requests. Safe defaults; never raises on a bad file."""
import copy, json, os, threading

DEFAULTS = {
    "notify": {
        "opened": True,        # a worker opens / applies a loan
        "disbursed": True,     # a worker disburses a loan
        "newAccount": True,    # a KR883 login this computer has not seen before
        "popup": True,         # Windows message box on this PC
        "dashboard": True,     # pop-up + sound on the dashboard page
        "sound": True,
    },
    "schedule": {"start": "09:00", "end": "18:00"},
    "idleMinutes": 15,
    "targets": {"repayments": 0},
    "rules": [],               # [{"key": "POST /api/loans/{id}/disburse/", "kind": "disbursed"}]
}
_LOCK = threading.Lock()


def _merge(base, over):
    for k, v in (over or {}).items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _merge(base[k], v)
        elif k in base and type(v) is type(base[k]):
            base[k] = v
    return base


def load(path):
    with _LOCK:
        s = copy.deepcopy(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as f:
                _merge(s, json.load(f))
        except Exception:
            pass
        return s


def save(path, patch):
    """Merge `patch` into the saved settings and return the result."""
    s = load(path)
    with _LOCK:
        _merge(s, patch)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    return s


def set_rule(path, key, kind):
    """Set (or clear, kind='') the user's rule for one request pattern."""
    s = load(path)
    rules = [r for r in s["rules"] if r.get("key") != key]
    if kind:
        rules.append({"key": key, "kind": kind})
    s["rules"] = rules
    with _LOCK:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    return s
