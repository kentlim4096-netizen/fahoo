"""Guided setup for the installed Credit Report Tool (tkinter - ships with Python).

Four steps:  1. KR883 login + authenticator (QR upload / scan / live code)   2. ngrok
             3. Panel logins   4. Connect Chrome (start it, check the session is visible)

Writes <home>\\config.env. Nothing is sent anywhere. The file lives in your own Windows user profile
(%LOCALAPPDATA%), so other Windows users cannot read it.

The hidden UI workflow never logs in for you: it mirrors the KR883 session of your normal Chrome, where
YOU log in (password + OTP - the live code in step 1 can replace your phone authenticator). Automatic
KR883 login stays OFF unless you set KW388_ALLOW_AUTO_LOGIN=true in config.env.
"""
import os, re, secrets, subprocess, sys, threading, time, webbrowser

import chrome_helper, qr_totp


# ------------------------------------------------------------------ validation / config (no GUI)
def totp_now(secret_b32):
    return qr_totp.code_now(qr_totp.parse_secret(secret_b32))[0]


def validate(v):
    """-> list of problems (empty = ok). v: dict of the form values."""
    bad = []
    ku = (v.get("kr_user") or "").strip()
    if ku and not re.fullmatch(r"[A-Za-z0-9._@-]{2,64}", ku):
        bad.append("KR883 username: letters, digits and . _ @ - only.")
    sec = (v.get("kr_totp") or "").strip()
    if sec:
        try:
            qr_totp.parse_secret(sec)
        except Exception:
            bad.append("The authenticator secret is not valid (Base32 or otpauth:// link expected).")
    dom = (v.get("ngrok_domain") or "").strip()
    if dom and not re.fullmatch(r"(https://)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.ngrok[\w.-]*", dom):
        bad.append("ngrok domain looks wrong (expected e.g. https://your-name.ngrok.app).")
    tok = (v.get("ngrok_token") or "").strip()
    if tok and not re.fullmatch(r"[A-Za-z0-9_]{20,}", tok):
        bad.append("ngrok token looks wrong (copy it from dashboard.ngrok.com/get-started/your-authtoken).")
    panel = [v.get(k) or "" for k in ("worker_user", "worker_pass", "admin_user", "admin_pass")]
    if dom or any(panel):                                   # public link => logins are mandatory
        def cred(prefix, label):
            u, p = v.get(prefix + "_user", ""), v.get(prefix + "_pass", "")
            if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", u or ""):
                bad.append(f"{label} username: 3-32 letters/digits (no spaces, no ':').")
            if len(p or "") < 8 or p != p.strip() or re.search(r"[\r\n]", p or ""):
                bad.append(f"{label} password: at least 8 characters, no leading/trailing spaces.")
        cred("worker", "Worker"); cred("admin", "Admin")
        if v.get("worker_user") and v.get("worker_user") == v.get("admin_user"):
            bad.append("Worker and admin usernames must differ.")
        if v.get("worker_pass") and v.get("worker_pass") == v.get("admin_pass"):
            bad.append("Worker and admin passwords must differ.")
    if not (v.get("chrome_profile") or "").strip():
        bad.append("Choose the Chrome profile you log in to KR883 with.")
    for k in ("kr_user", "kr_pass", "ngrok_domain", "chrome_profile"):
        if re.search(r"[\r\n]", v.get(k, "") or ""):
            bad.append("Line breaks are not allowed in any field.")
    return bad


def render_config(v):
    dom = (v.get("ngrok_domain") or "").strip()
    if dom and not dom.startswith("https://"):
        dom = "https://" + dom
    has_panel = bool(v.get("worker_user") and v.get("admin_user"))
    sec = ""
    if (v.get("kr_totp") or "").strip():
        sec = qr_totp.parse_secret(v["kr_totp"]).secret
    return "\n".join([
        "# Credit Report Tool settings (written by the setup window). Keep this file private.",
        "CREDIT_REPORT_MODE=ui",
        "KW388_BASE_URL=https://admin.kr883.com",
        "KW388_CDP_URL=http://127.0.0.1:9222",
        f"KW388_CHROME_PROFILE={(v.get('chrome_profile') or 'Default').strip()}",
        "KW388_FOLLOW_BROWSER=true",
        "KW388_AUTO_OPEN_CHROME=false",
        "KW388_ALLOW_AUTO_LOGIN=false",
        "KW388_UI_HEADLESS=true",
        "KW388_BIND=127.0.0.1",
        "LOCAL_ONLY=true",
        "TZ=Asia/Kuala_Lumpur",
        "KW_HUMAN_DELAY_MIN=0",
        "KW_HUMAN_DELAY_MAX=0",
        f"KW388_WORKER_AUTH={v['worker_user'] + ':' + v['worker_pass'] if has_panel else ''}",
        f"KW388_ADMIN_AUTH={v['admin_user'] + ':' + v['admin_pass'] if has_panel else ''}",
        f"NGROK_DOMAIN={dom}",
        "# KR883 details. The hidden workflow does not use them; the TOTP secret feeds the code generator.",
        f"KW388_USERNAME={(v.get('kr_user') or '').strip()}",
        f"KW388_PASSWORD={v.get('kr_pass') or ''}",
        f"KW388_TOTP_SECRET={sec}",
        "",
    ])


def read_config(home):
    """Existing config.env -> the wizard's field values (for re-running Settings)."""
    path = os.path.join(home, "config.env")
    kv = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, val = line.rstrip("\n").split("=", 1)
                kv[k.strip()] = val.strip()
    w, a = kv.get("KW388_WORKER_AUTH", ""), kv.get("KW388_ADMIN_AUTH", "")
    return {"kr_user": kv.get("KW388_USERNAME", ""), "kr_pass": kv.get("KW388_PASSWORD", ""),
            "kr_totp": kv.get("KW388_TOTP_SECRET", ""), "ngrok_domain": kv.get("NGROK_DOMAIN", ""),
            "worker_user": w.split(":", 1)[0] if ":" in w else "", "worker_pass": w.split(":", 1)[1] if ":" in w else "",
            "admin_user": a.split(":", 1)[0] if ":" in a else "", "admin_pass": a.split(":", 1)[1] if ":" in a else "",
            "chrome_profile": kv.get("KW388_CHROME_PROFILE", "Default")}


def save(home, bundle_dir, v):
    """Write config.env (and register the ngrok token with ngrok's own config). Returns a note."""
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "config.env"), "w", encoding="utf-8", newline="\n") as f:
        f.write(render_config(v))
    note = ""
    tok = (v.get("ngrok_token") or "").strip()
    if tok:
        exe = os.path.join(bundle_dir, "tools", "bin", "ngrok.exe")
        try:
            r = subprocess.run([exe, "config", "add-authtoken", tok], capture_output=True, text=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            note = "ngrok token saved." if r.returncode == 0 else "ngrok token was NOT saved: " + (r.stderr or r.stdout)[:120]
        except Exception as e:
            note = "ngrok token was NOT saved: " + str(e)[:120]
    return note


# ------------------------------------------------------------------ GUI
class CodeWidget:
    """Live authenticator code with a countdown bar. Re-reads the secret from a tk variable."""

    def __init__(self, parent, tk, ttk, secret_var, big=28):
        self.tk, self.var = tk, secret_var
        self.frame = ttk.Frame(parent)
        self.code = ttk.Label(self.frame, text="--- ---", font=("Consolas", big, "bold"))
        self.code.grid(row=0, column=0, sticky="w")
        self.copy = ttk.Button(self.frame, text="Copy", width=7, command=self._copy)
        self.copy.grid(row=0, column=1, padx=(12, 0))
        self.bar = ttk.Progressbar(self.frame, length=220, maximum=30)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.info = ttk.Label(self.frame, text="", foreground="#666")
        self.info.grid(row=2, column=0, columnspan=2, sticky="w")
        self._last = ""
        self._tick()

    def _tick(self):
        try:
            if not self.frame.winfo_exists():
                return
            s = (self.var.get() or "").strip()
            if s:
                ts = qr_totp.parse_secret(s)
                code, left = qr_totp.code_now(ts)
                self._last = code
                self.code.config(text=code[:3] + " " + code[3:] if len(code) == 6 else code, foreground="#000")
                self.bar.config(maximum=ts.period, value=left)
                self.info.config(text=f"changes in {left} s" + ("" if ts.is_default_kind else " - unusual code type, check KR883 accepts it"))
            else:
                self._last = ""
                self.code.config(text="--- ---", foreground="#aaa")
                self.bar.config(value=0)
                self.info.config(text="No authenticator added yet.")
        except Exception:
            self._last = ""
            self.code.config(text="invalid", foreground="#b00")
            self.info.config(text="That is not a valid secret.")
        self.frame.after(500, self._tick)

    def _copy(self):
        if self._last:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(self._last)
            self.copy.config(text="Copied")
            self.frame.after(1500, lambda: self.copy.config(text="Copy"))


def _mk_root():
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    root.option_add("*Font", "{Segoe UI} 10")
    return tk, ttk, root


def run_wizard(home, bundle_dir, on_root=None):
    """Show the guided setup. Returns True if settings were saved, False if cancelled.
    on_root(wizard) is a test hook called once the window exists."""
    tk, ttk, root = _mk_root()
    w = Wizard(tk, ttk, root, home, bundle_dir)
    if on_root:
        on_root(w)
    root.mainloop()
    return w.saved


class Wizard:
    STEPS = ("1  KR883 login", "2  ngrok", "3  Panel logins", "4  Connect Chrome")

    def __init__(self, tk, ttk, root, home, bundle_dir):
        from tkinter import filedialog, messagebox
        self.tk, self.ttk, self.root, self.home, self.bundle = tk, ttk, root, home, bundle_dir
        self.fd, self.mb = filedialog, messagebox
        self.saved = False
        root.title("Credit Report Tool - setup")
        root.geometry("640x600")
        root.minsize(640, 600)
        pre = read_config(home)
        self.v = {k: tk.StringVar(value=val) for k, val in
                  {**{"ngrok_token": ""}, **pre}.items()}
        self.v["show"] = tk.IntVar(value=0)
        outer = ttk.Frame(root, padding=(18, 14))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Set up the Credit Report Tool", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        self.crumb = ttk.Label(outer, text="", foreground="#666")
        self.crumb.pack(anchor="w", pady=(0, 8))
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)
        self.pages = [self._page1(), self._page2(), self._page3(), self._page4()]
        for p, title in zip(self.pages, self.STEPS):
            self.nb.add(p, text=title)
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self._sync_nav())
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(12, 0))
        self.b_cancel = ttk.Button(bar, text="Cancel", command=root.destroy)
        self.b_cancel.pack(side="left")
        self.b_next = ttk.Button(bar, text="Next  >", command=self.next)
        self.b_next.pack(side="right")
        self.b_back = ttk.Button(bar, text="<  Back", command=self.back)
        self.b_back.pack(side="right", padx=8)
        self._sync_nav()

    # ---- helpers
    def _grid_field(self, parent, r, label, key, secret=False, width=38):
        self.ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
        e = self.ttk.Entry(parent, textvariable=self.v[key], width=width, show="*" if secret else "")
        e.grid(row=r, column=1, sticky="w", pady=4)
        if secret:
            self.secret_entries.append(e)
        return e

    def _note(self, parent, r, text, colour="#666", span=3):
        lb = self.ttk.Label(parent, text=text, foreground=colour, wraplength=560, justify="left")
        lb.grid(row=r, column=0, columnspan=span, sticky="w", pady=(2, 6))
        return lb

    secret_entries = []

    # ---- pages
    def _page1(self):
        tk, ttk = self.tk, self.ttk
        self.secret_entries = []
        p = ttk.Frame(self.nb, padding=14)
        self._note(p, 0, "Your KR883 sign-in. You log in yourself in Chrome (step 4); the hidden workflow then reuses that "
                         "session. The authenticator below can replace your phone app.")
        self._grid_field(p, 1, "KR883 username (ID)", "kr_user")
        self._grid_field(p, 2, "KR883 password (optional)", "kr_pass", secret=True)
        ttk.Separator(p).grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Label(p, text="Authenticator (TOTP)", font=("Segoe UI", 11, "bold")).grid(row=4, column=0, columnspan=3, sticky="w")
        self._note(p, 5, "Open KR883's 2-step setup so the QR code is visible, then click 'Scan QR on screen'. "
                         "Or upload a screenshot / photo of the QR.")
        bf = ttk.Frame(p)
        bf.grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Button(bf, text="Upload QR image...", command=self.upload_qr).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(bf, text="Scan QR on screen", command=self.scan_qr).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(bf, text="Clear", command=lambda: (self.v["kr_totp"].set(""), self._qr_status("", "#666"))).grid(row=0, column=2)
        self.qr_lbl = self._note(p, 7, "")
        mf = ttk.Frame(p)
        mf.grid(row=8, column=0, columnspan=3, sticky="w")
        ttk.Label(mf, text="or paste the secret / otpauth link:").grid(row=0, column=0, padx=(0, 8))
        self.paste_var = tk.StringVar()
        ttk.Entry(mf, textvariable=self.paste_var, width=30, show="*").grid(row=0, column=1)
        ttk.Button(mf, text="Use", width=5, command=self.use_pasted).grid(row=0, column=2, padx=(6, 0))
        self.code_widget = CodeWidget(p, tk, ttk, self.v["kr_totp"])
        self.code_widget.frame.grid(row=9, column=0, columnspan=3, sticky="w", pady=(14, 0))
        if self.v["kr_totp"].get():
            self._qr_status("Authenticator already saved - scan again to replace it.", "#0a6")
        return p

    def _page2(self):
        ttk = self.ttk
        p = ttk.Frame(self.nb, padding=14)
        self._note(p, 0, "Optional - gives this computer a public link so workers can use the Credit Report page from anywhere. "
                         "Skip it to use the tool on this computer only.")
        self._grid_field(p, 1, "ngrok authtoken", "ngrok_token", secret=True)
        self._grid_field(p, 2, "ngrok domain", "ngrok_domain")
        bf = ttk.Frame(p)
        bf.grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(bf, text="Where do I get the token?", command=lambda: webbrowser.open("https://dashboard.ngrok.com/get-started/your-authtoken")).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(bf, text="Where do I get a domain?", command=lambda: webbrowser.open("https://dashboard.ngrok.com/domains")).grid(row=0, column=1)
        self._note(p, 4, "Your domain looks like  your-name.ngrok.app  (free accounts get one). A domain can be online on ONE "
                         "computer at a time. When a public link is used, step 3 (panel logins) is required.")
        return p

    def _page3(self):
        ttk = self.ttk
        p = ttk.Frame(self.nb, padding=14)
        self._note(p, 0, "Sign-in for this tool's web page. Worker = Credit Report page only. Admin = everything. "
                         "Leave all four blank only if this computer alone uses it and no ngrok link is set.")
        self._grid_field(p, 1, "Worker username", "worker_user"); self._grid_field(p, 2, "Worker password", "worker_pass", secret=True)
        ttk.Button(p, text="Generate", command=lambda: self._gen("worker_user", "worker_pass", "worker")).grid(row=2, column=2, padx=8)
        self._grid_field(p, 3, "Admin username", "admin_user"); self._grid_field(p, 4, "Admin password", "admin_pass", secret=True)
        ttk.Button(p, text="Generate", command=lambda: self._gen("admin_user", "admin_pass", "admin")).grid(row=4, column=2, padx=8)
        ttk.Checkbutton(p, text="Show passwords", variable=self.v["show"], command=self._toggle_show).grid(row=5, column=1, sticky="w", pady=8)
        self._note(p, 6, "Passwords need at least 8 characters. Write them down - they are stored only on this computer.")
        return p

    def _page4(self):
        tk, ttk = self.tk, self.ttk
        p = ttk.Frame(self.nb, padding=14)
        self._note(p, 0, "The hidden workflow reads your KR883 session from your normal Chrome. Pick the Chrome profile you use for KR883, "
                         "start Chrome with the button below, log in to KR883 there yourself, and keep that tab open.")
        ttk.Label(p, text="Chrome profile").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 10))
        profs = chrome_helper.list_profiles()
        self.prof_map = {label: folder for folder, label in profs}
        cur = chrome_helper.default_profile(self.v["chrome_profile"].get() or None)
        self.prof_var = tk.StringVar(value=next((l for f, l in profs if f == cur), profs[0][1]))
        ttk.Combobox(p, textvariable=self.prof_var, values=[l for _f, l in profs], width=38, state="readonly").grid(row=1, column=1, sticky="w")
        bf = ttk.Frame(p)
        bf.grid(row=2, column=0, columnspan=2, sticky="w", pady=10)
        ttk.Button(bf, text="Start Chrome for KR883", command=self.start_chrome).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(bf, text="Check connection", command=self.check_connection).grid(row=0, column=1)
        self.conn_lbl = self._note(p, 3, "", span=2)
        self._note(p, 4, "You can finish now and connect later - Start Menu > 'Chrome for KR883 login'.")
        return p

    # ---- actions
    def _qr_status(self, text, colour):
        self.qr_lbl.config(text=text, foreground=colour)

    def _apply_secret(self, ts, source):
        self.v["kr_totp"].set(ts.secret)
        who = " ".join(x for x in (ts.issuer, ts.account) if x)
        self._qr_status(f"Authenticator added from {source}" + (f" ({who})" if who else "") + ". Compare the code below with your app.", "#0a6")
        if not self.v["kr_user"].get().strip() and ts.account:
            self.v["kr_user"].set(re.sub(r"^.*:", "", ts.account))

    def upload_qr(self):
        path = self.fd.askopenfilename(title="Choose the QR code image", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.webp"), ("All files", "*.*")])
        if not path:
            return
        try:
            ts = qr_totp.qr_from_file(path)
        except Exception as e:
            return self._qr_status(str(e), "#b00")
        self._apply_secret(ts, "the image") if ts else self._qr_status("No QR code found in that image. Try a sharper / larger screenshot.", "#b00")

    def scan_qr(self):
        self.root.withdraw()                                  # get our own window out of the way
        self.root.update()
        time.sleep(0.4)
        try:
            ts = qr_totp.qr_from_screen()
            err = None
        except Exception as e:
            ts, err = None, str(e)
        self.root.deiconify()
        self.root.lift()
        if err:
            self._qr_status(err, "#b00")
        elif ts:
            self._apply_secret(ts, "your screen")
        else:
            self._qr_status("No QR code found on screen. Show the QR fully (not covered) and try again.", "#b00")

    def use_pasted(self):
        try:
            ts = qr_totp.parse_secret(self.paste_var.get())
        except Exception as e:
            return self._qr_status(str(e) or "Not a valid secret.", "#b00")
        self.paste_var.set("")
        self._apply_secret(ts, "the pasted text")

    def _gen(self, ukey, pkey, prefix):
        if not self.v[ukey].get().strip():
            self.v[ukey].set(prefix + str(secrets.randbelow(900) + 100))
        self.v[pkey].set(secrets.token_urlsafe(9))
        self.v["show"].set(1)
        self._toggle_show()

    def _toggle_show(self):
        for e in self.secret_entries:
            e.config(show="" if self.v["show"].get() else "*")

    def _profile_folder(self):
        return self.prof_map.get(self.prof_var.get(), self.prof_var.get().split(" - ")[0])

    def start_chrome(self):
        ok, msg = chrome_helper.start_debug_chrome(self._profile_folder())
        self.conn_lbl.config(text=("[ok] " if ok else "[!] ") + msg, foreground="#0a6" if ok else "#b00")

    def check_connection(self):
        self.conn_lbl.config(text="Checking...", foreground="#666")
        out = {}

        def work():
            import asyncio
            from session_sync import SessionSync, seconds_left
            try:
                if not chrome_helper.debug_port_open():
                    out["r"] = ("[!] Chrome is not running with the debug port. Click 'Start Chrome for KR883' first.", "#b00")
                    return
                found = asyncio.run(SessionSync("https://admin.kr883.com").find_source())
                if found:
                    import json as _json
                    who, warn = "", ""
                    try:
                        prof = _json.loads(found[0].get("kw388_profile") or "null") or {}
                        perms, sup = prof.get("permissions") or [], bool((prof.get("adminProfile") or {}).get("is_super_admin"))
                        who = f"{(prof.get('user') or {}).get('username', '?')} ({(prof.get('position') or {}).get('name', '?')})"
                        miss = [n for n in ("view_member_list", "view_credit_report") if not sup and n not in perms]
                        if miss:
                            warn = "  WARNING: this KR883 ID lacks " + " and ".join(miss) + " - lookups will fail."
                    except Exception:
                        pass
                    mins = int(seconds_left(found[0]['kw388_access']) // 60)
                    out["r"] = (f"[{'!' if warn else 'ok'}] Logged in to KR883 as {who or 'unknown'} - session valid for about {mins} min." + (warn or " All set."),
                                "#b00" if warn else "#0a6")
                else:
                    out["r"] = ("[!] Chrome is open, but no logged-in KR883 tab was found. Log in to admin.kr883.com in that Chrome "
                                "and keep the tab open, then check again.", "#b00")
            except Exception as e:
                out["r"] = ("[!] Could not check: " + str(e)[:120], "#b00")
        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if "r" in out:
                self.conn_lbl.config(text=out["r"][0], foreground=out["r"][1])
            else:
                self.root.after(300, poll)
        poll()

    # ---- navigation
    def values(self):
        v = {k: x.get() for k, x in self.v.items() if k != "show"}
        v["chrome_profile"] = self._profile_folder()
        return v

    def _sync_nav(self):
        i = self.nb.index(self.nb.select())
        self.crumb.config(text=f"Step {i + 1} of {len(self.STEPS)}")
        self.b_back.state(["!disabled"] if i > 0 else ["disabled"])
        self.b_next.config(text="Finish" if i == len(self.STEPS) - 1 else "Next  >")

    def next(self):
        i = self.nb.index(self.nb.select())
        if i < len(self.STEPS) - 1:
            self.nb.select(i + 1)
        else:
            self.finish()

    def back(self):
        i = self.nb.index(self.nb.select())
        if i > 0:
            self.nb.select(i - 1)

    def finish(self):
        v = self.values()
        problems = validate(v)
        if problems:
            self.mb.showerror("Please fix", "\n".join(problems))
            return False
        try:
            note = save(self.home, self.bundle, v)
        except Exception as e:
            self.mb.showerror("Could not save", str(e))
            return False
        self.saved = True
        self.mb.showinfo("All set", "Settings saved." + (("\n" + note) if note else "") +
                         "\n\nThe tool now runs hidden in the background and opens its web page.")
        self.root.destroy()
        return True


def run_totp_window(home):
    """Small always-on-top window that shows the live authenticator code (replaces the phone app)."""
    tk, ttk, root = _mk_root()
    root.title("KR883 authenticator")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    cfg = read_config(home)
    var = tk.StringVar(value=cfg.get("kr_totp", ""))
    f = ttk.Frame(root, padding=18)
    f.pack()
    ttk.Label(f, text=("KR883  " + cfg["kr_user"]).strip(), font=("Segoe UI", 11, "bold")).pack(anchor="w")
    CodeWidget(f, tk, ttk, var, big=36).frame.pack(anchor="w", pady=8)
    if not cfg.get("kr_totp"):
        ttk.Label(f, text="No authenticator saved yet.\nOpen Start Menu > Credit Report Tool - Settings.", foreground="#b00").pack(anchor="w")
    root.mainloop()
