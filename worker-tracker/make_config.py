"""Create config.json (the shared key) on the tracker PC. Safe to re-run: never overwrites an existing key."""
import json, os, secrets

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
if os.path.exists(p):
    print("config.json already exists - key unchanged.")
else:
    json.dump({"port": 8780, "key": secrets.token_hex(24)}, open(p, "w", encoding="utf-8"))
    print("Created config.json. The key inside is what each worker PC needs for install_extension.py --key")
