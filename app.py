#!/usr/bin/env python3
"""MultiNode SSH Console: run one command on many SSH hosts and read each log.

Written by Zareh Kasparian with Claude Code support.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

import core

VERSION = "1.2.11"
APP_TITLE = "MultiNode SSH Console"
CREDIT = "Written by Zareh Kasparian with Claude Code support."
ALL_ROW = "__all__"
COMPANY_LOGO_FILE = "systemgroup_banner.png"
ANTHROPIC_LOGO_FILE = "anthropic_logo.png"


def resource_path(relative: str) -> str:
    """Path of a bundled data file (works from source and from a PyInstaller exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)
STATUS_LABELS = {
    "Pending": "Pending",
    "Running": "Running…",
    core.OK: "✔ Done",
    core.FAILED: "✖ Failed",
    core.ERROR: "✖ Error",
    core.TIMEOUT: "✖ Timeout",
    core.UNREACHABLE: "✖ Unreachable",
    core.CANCELLED: "Cancelled",
}
_GREEN = ("#052e16", "#bbf7d0")  # (text, background): command completed successfully
_RED = ("#7f1d1d", "#fecaca")  # command failed / host unreachable
STATUS_STYLE = {
    "Pending": ("#4b5563", "#f3f4f6"),
    "Running": ("#1e3a8a", "#dbeafe"),
    core.OK: _GREEN,
    core.FAILED: _RED,
    core.ERROR: _RED,
    core.TIMEOUT: _RED,
    core.UNREACHABLE: _RED,
    core.CANCELLED: ("#4b5563", "#e5e7eb"),
}
# Progress bar chunk colours.
CUBE_COLOURS = {
    "Pending": "#d1d5db",
    "Running": "#86efac",
    core.OK: "#16a34a",
    core.FAILED: "#dc2626",
    core.ERROR: "#dc2626",
    core.TIMEOUT: "#dc2626",
    core.UNREACHABLE: "#dc2626",
    core.CANCELLED: "#9ca3af",
}
FAILED_STATUSES = (core.FAILED, core.ERROR, core.TIMEOUT, core.UNREACHABLE)


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=8)
        self.root = root
        self.pack(fill="both", expand=True)
        self.results: dict[str, core.Result] = {}
        self.running: set[str] = set()
        self.order: list[str] = []
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self._build()
        self.root.after(100, self._poll)

    # ------------------------------------------------------------ layout
    def _build(self):
        # Packed first so the pane below can't squeeze it out of the window.
        footer = ttk.Frame(self)
        footer.pack(side="bottom", anchor="e", pady=(6, 0))
        ttk.Label(footer, text=CREDIT, foreground="#6b7280").pack(side="left")
        try:
            # Kept as an attribute so Tk doesn't garbage-collect the image.
            self.logo_anthropic = tk.PhotoImage(
                file=resource_path(os.path.join("assets", ANTHROPIC_LOGO_FILE))
            )
            ttk.Label(footer, image=self.logo_anthropic).pack(side="left", padx=(8, 0))
        except tk.TclError:
            pass  # logo is optional
        banner = ttk.Frame(self)
        banner.pack(side="top", fill="x", pady=(0, 6))
        banner.columnconfigure(2, weight=1)

        def logo(column: int, filename: str, sticky: str, padx=0):
            """Show a bundled logo in the banner (silently skipped if missing)."""
            try:
                img = tk.PhotoImage(file=resource_path(os.path.join("assets", filename)))
            except tk.TclError:
                return None
            ttk.Label(banner, image=img).grid(row=0, column=column, sticky=sticky, padx=padx)
            return img

        # Kept as attributes so Tk doesn't garbage-collect the images.
        self.logo_left = logo(0, COMPANY_LOGO_FILE, "w", padx=(0, 10))
        titles = ttk.Frame(banner)
        titles.grid(row=0, column=1, sticky="w")
        ttk.Label(titles, text=APP_TITLE, font=("TkDefaultFont", 15, "bold")).pack(anchor="w")
        ttk.Label(titles, text=f"Version {VERSION}", foreground="#6b7280").pack(anchor="w")
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=(0, 0, 8, 0))
        right = ttk.Frame(paned)
        paned.add(left, weight=0)
        paned.add(right, weight=1)

        # Connection ------------------------------------------------
        conn = ttk.LabelFrame(left, text="Connection", padding=8)
        conn.pack(fill="x")
        conn.columnconfigure(1, weight=1)
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.port = tk.StringVar(value="22")
        self.connect_timeout = tk.StringVar(value="10")
        self.command_timeout = tk.StringVar(value="60")
        self.workers = tk.StringVar(value="10")
        self.precheck = tk.BooleanVar(value=True)
        self.separate = tk.BooleanVar(value=True)
        self.stop_on_error = tk.BooleanVar(value=True)
        self.precheck_timeout = tk.StringVar(value="3")
        self.started_at = 0.0
        self.use_shell = tk.BooleanVar(value=False)
        self.verify_keys = tk.BooleanVar(value=True)
        self.trust_known_keys = tk.BooleanVar(value=False)
        # Two label+entry pairs per row keep this box short.
        def field(row, col, label, var, show="", width=8, span=1):
            ttk.Label(conn, text=label).grid(row=row, column=col * 2, sticky="w", pady=2)
            ttk.Entry(conn, textvariable=var, show=show, width=width).grid(
                row=row, column=col * 2 + 1, columnspan=span * 2 - 1, sticky="ew",
                padx=(6, 10 if col == 0 and span == 1 else 0), pady=2,
            )

        conn.columnconfigure(1, weight=1)
        conn.columnconfigure(3, weight=1)
        field(0, 0, "Username", self.username, span=2, width=24)
        field(1, 0, "Password", self.password, show="*", span=2, width=24)
        field(2, 0, "Port", self.port)
        field(2, 1, "Parallel", self.workers)
        field(3, 0, "Connect (s)", self.connect_timeout)
        field(3, 1, "Command (s)", self.command_timeout)

        pre = ttk.Frame(conn)
        pre.grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(
            pre, text="Check SSH port is reachable first, wait", variable=self.precheck
        ).pack(side="left")
        ttk.Entry(pre, textvariable=self.precheck_timeout, width=4).pack(side="left", padx=4)
        ttk.Label(pre, text="s").pack(side="left")
        ttk.Checkbutton(
            conn, text="Interactive shell mode (devices that reject exec)",
            variable=self.use_shell,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # Host keys -------------------------------------------------
        keys = ttk.LabelFrame(left, text="Host keys (SSH public key)", padding=8)
        keys.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(
            keys, text="Ask me to accept a new device's SSH key",
            variable=self.verify_keys,
        ).pack(anchor="w")
        ttk.Checkbutton(
            keys, text="Trust a known key that shows up on a new IP",
            variable=self.trust_known_keys,
        ).pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(keys)
        row.pack(fill="x", pady=(6, 0))
        ttk.Button(row, text="View accepted keys…", command=self.show_known_hosts).pack(side="left")

        # Targets ---------------------------------------------------
        tgt = ttk.LabelFrame(left, text="Targets (IP or hostname)", padding=8)
        tgt.pack(fill="both", expand=True, pady=8)
        ttk.Label(
            tgt, text="One per line, or comma/space separated. host:port allowed.",
            foreground="#6b7280",
        ).pack(anchor="w")
        box = ttk.Frame(tgt)
        box.pack(fill="both", expand=True, pady=4)
        self.targets_text = tk.Text(box, height=5, width=34, wrap="none", undo=True)
        sb = ttk.Scrollbar(box, command=self.targets_text.yview)
        self.targets_text.configure(yscrollcommand=sb.set)
        self.targets_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.targets_text.bind("<<Modified>>", self._targets_modified)
        bar = ttk.Frame(tgt)
        bar.pack(fill="x")
        ttk.Button(bar, text="Import from file…", command=self.import_file).pack(side="left")
        ttk.Button(bar, text="Clear", command=self.clear_targets).pack(side="left", padx=4)
        self.target_count = ttk.Label(bar, text="0 targets")
        self.target_count.pack(side="right")

        # Command ---------------------------------------------------
        cmd = ttk.LabelFrame(left, text="Commands to run", padding=8)
        cmd.pack(fill="x")
        head = ttk.Frame(cmd)
        head.pack(fill="x")
        ttk.Label(
            head, text="One command per line (# = comment)", foreground="#6b7280"
        ).pack(side="left")
        ttk.Button(head, text="Load file…", command=self.load_commands).pack(side="right")
        preset_row = ttk.Frame(cmd)
        preset_row.pack(fill="x", pady=(4, 0))
        self.preset = tk.StringVar()
        preset_box = ttk.Combobox(
            preset_row, textvariable=self.preset, state="readonly",
            values=[label for label, _ in core.APC_PRESETS], width=24,
        )
        preset_box.pack(side="left", fill="x", expand=True)
        preset_box.bind("<<ComboboxSelected>>", lambda _e: self.add_preset())
        ttk.Label(preset_row, text="APC PDU presets", foreground="#6b7280").pack(
            side="left", padx=(6, 0)
        )
        box = ttk.Frame(cmd)
        box.pack(fill="x", pady=(4, 0))
        self.command_text = tk.Text(box, height=4, width=34, wrap="none", undo=True)
        csb = ttk.Scrollbar(box, command=self.command_text.yview)
        self.command_text.configure(yscrollcommand=csb.set)
        self.command_text.pack(side="left", fill="x", expand=True)
        csb.pack(side="right", fill="y")
        opts = ttk.Frame(cmd)
        opts.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(opts, text="Each line is a separate command", variable=self.separate).pack(anchor="w")
        ttk.Checkbutton(opts, text="Stop on the first failing command", variable=self.stop_on_error).pack(anchor="w")

        actions = ttk.Frame(left)
        actions.pack(fill="x", pady=(8, 0))
        self.run_btn = ttk.Button(actions, text="Run on all targets", command=self.run)
        self.run_btn.pack(side="left", fill="x", expand=True)
        self.test_btn = ttk.Button(
            actions, text="Test login (dry run)", command=lambda: self.run(dry_run=True)
        )
        self.test_btn.pack(side="left", padx=(6, 0))
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))

        # Results ---------------------------------------------------
        res = ttk.LabelFrame(right, text="Results", padding=8)
        res.pack(fill="both", expand=True)
        vpaned = ttk.PanedWindow(res, orient="vertical")
        vpaned.pack(fill="both", expand=True)

        top = ttk.Frame(vpaned)
        cols = ("status", "exit", "time")
        self.tree = ttk.Treeview(top, columns=cols, show="tree headings", height=8)
        self.tree.heading("#0", text="Host")
        self.tree.heading("status", text="Status")
        self.tree.heading("exit", text="Exit")
        self.tree.heading("time", text="Time")
        self.tree.column("#0", width=220)
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("exit", width=50, anchor="center")
        self.tree.column("time", width=70, anchor="e")
        for status, (fg, bg) in STATUS_STYLE.items():
            self.tree.tag_configure(status, foreground=fg, background=bg)
        tsb = ttk.Scrollbar(top, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())
        vpaned.add(top, weight=1)

        bottom = ttk.Frame(vpaned)
        self.output = tk.Text(bottom, wrap="none", state="disabled", font="TkFixedFont")
        for status, (fg, bg) in STATUS_STYLE.items():
            self.output.tag_configure(status, foreground=fg, background=bg)
        self.output.tag_configure("cmd", foreground="#1d4ed8", font=("TkFixedFont", 10, "bold"))
        osb = ttk.Scrollbar(bottom, command=self.output.yview)
        osb_x = ttk.Scrollbar(bottom, orient="horizontal", command=self.output.xview)
        self.output.configure(yscrollcommand=osb.set, xscrollcommand=osb_x.set)
        osb_x.pack(side="bottom", fill="x")
        osb.pack(side="right", fill="y")
        self.output.pack(side="left", fill="both", expand=True)
        vpaned.add(bottom, weight=3)

        foot = ttk.Frame(res)
        foot.pack(fill="x", pady=(6, 0))
        self.summary = ttk.Label(foot, text="Ready", width=44)
        self.summary.pack(side="left")
        self.ok_label = tk.Label(
            foot, text="OK: 0", fg=STATUS_STYLE[core.OK][0], bg=STATUS_STYLE[core.OK][1],
            font=("TkDefaultFont", 10, "bold"), padx=8, pady=1,
        )
        self.ok_label.pack(side="left", padx=(8, 4))
        self.failed_label = tk.Label(
            foot, text="Failed: 0", fg=STATUS_STYLE[core.FAILED][0], bg=STATUS_STYLE[core.FAILED][1],
            font=("TkDefaultFont", 10, "bold"), padx=8, pady=1,
        )
        self.failed_label.pack(side="left", padx=(0, 8))
        # Buttons are packed before the cubes so the cubes only take leftover space.
        ttk.Button(foot, text="Save logs…", command=self.save_logs).pack(side="right")
        ttk.Button(foot, text="Copy", command=self.copy_output).pack(side="right", padx=4)
        self.cubes = tk.Canvas(foot, width=200, height=18, highlightthickness=0, bd=0)
        self.cubes.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.cubes.bind("<Configure>", lambda _e: self._draw_bar())

    # ------------------------------------------------------------ targets
    def _default_port(self) -> int | None:
        try:
            port = int(self.port.get())
        except ValueError:
            return None
        return port if 1 <= port <= 65535 else None

    def _targets_modified(self, _event=None):
        if not self.targets_text.edit_modified():
            return
        self.targets_text.edit_modified(False)
        targets, _ = core.parse_targets(self.targets_text.get("1.0", "end"), self._default_port() or 22)
        n = len(targets)
        self.target_count.configure(text=f"{n} target{'s' if n != 1 else ''}")

    def add_preset(self):
        """Append the selected preset to the command box (asking for <n>)."""
        label = self.preset.get()
        self.preset.set("")
        template = dict(core.APC_PRESETS).get(label)
        if not template:
            return
        if "<n>" in template:
            n = simpledialog.askstring(
                APP_TITLE, f"{label}\n\nOutlet number (e.g. 3, 1-4) or all:", parent=self.root
            )
            if not n or not n.strip():
                return
            template = template.replace("<n>", n.strip())
        existing = self.command_text.get("1.0", "end").strip()
        self.command_text.delete("1.0", "end")
        self.command_text.insert("1.0", (existing + "\n" if existing else "") + template)

    def load_commands(self):
        path = filedialog.askopenfilename(
            title="Load commands",
            filetypes=[("Text files", "*.txt *.cmd *.sh *.conf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not read file:\n{e}")
            return
        existing = self.command_text.get("1.0", "end").strip()
        if existing and not messagebox.askyesno(
            APP_TITLE, "Replace the commands currently in the box?\n\nYes = replace, No = append."
        ):
            content = existing + "\n" + content
        self.command_text.delete("1.0", "end")
        self.command_text.insert("1.0", content.strip("\n") + "\n")

    def clear_targets(self):
        self.targets_text.delete("1.0", "end")

    def import_file(self):
        path = filedialog.askopenfilename(
            title="Import IP addresses",
            filetypes=[("Text / CSV files", "*.txt *.csv *.lst *.list"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not read file:\n{e}")
            return
        targets, errors = core.parse_targets(content, self._default_port() or 22)
        existing = self.targets_text.get("1.0", "end").strip()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self.targets_text.delete("1.0", "end")
        self.targets_text.insert("1.0", existing + "\n".join(self._format(t) for t in targets) + "\n")
        msg = f"Imported {len(targets)} address{'es' if len(targets) != 1 else ''}."
        if errors:
            shown = "\n".join(f"  {tok}: {why}" for tok, why in errors[:15])
            more = f"\n  … and {len(errors) - 15} more" if len(errors) > 15 else ""
            msg += f"\n\nSkipped {len(errors)} invalid entr{'ies' if len(errors) != 1 else 'y'}:\n{shown}{more}"
        messagebox.showinfo(APP_TITLE, msg)

    def _format(self, t: core.Target) -> str:
        """Write the port only when it differs from the default port field."""
        if t.port == (self._default_port() or 22):
            return t.host
        return str(t)

    # ------------------------------------------------------------ running
    def run(self, dry_run: bool = False):
        if self.worker and self.worker.is_alive():
            return
        username = self.username.get().strip()
        if not username:
            messagebox.showwarning(APP_TITLE, "Enter a username.")
            return
        default_port = self._default_port()
        if default_port is None:
            messagebox.showwarning(APP_TITLE, "Port must be a number between 1 and 65535.")
            return
        try:
            connect_t = float(self.connect_timeout.get())
            command_t = float(self.command_timeout.get())
            workers = int(self.workers.get())
            precheck_t = float(self.precheck_timeout.get())
            if connect_t <= 0 or command_t <= 0 or workers < 1 or precheck_t <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(
                APP_TITLE, "Timeouts must be positive numbers and parallel connections at least 1."
            )
            return
        command = self.command_text.get("1.0", "end").strip()
        commands = core.split_commands(command, self.separate.get())
        if not commands and not dry_run:
            messagebox.showwarning(APP_TITLE, "Enter at least one command to run.")
            return
        targets, errors = core.parse_targets(self.targets_text.get("1.0", "end"), default_port)
        if errors:
            shown = "\n".join(f"  {tok}: {why}" for tok, why in errors[:15])
            if not messagebox.askokcancel(
                APP_TITLE, f"{len(errors)} invalid entr{'ies' if len(errors) != 1 else 'y'} will be skipped:\n{shown}\n\nContinue?"
            ):
                return
        if not targets:
            messagebox.showwarning(APP_TITLE, "Add at least one IP address or hostname.")
            return
        risky = [] if dry_run else core.disruptive_commands(commands)
        if risky and not messagebox.askyesno(
            APP_TITLE,
            "These commands change power or reboot devices:\n\n  "
            + "\n  ".join(risky[:8]) + ("\n  …" if len(risky) > 8 else "")
            + f"\n\nRun on {len(targets)} host{'s' if len(targets) != 1 else ''}?",
            icon="warning", default="no",
        ):
            return
        if not dry_run and not risky and len(targets) > 1 and not messagebox.askokcancel(
            APP_TITLE,
            f"Run {len(commands)} command{'s' if len(commands) != 1 else ''} "
            f"on {len(targets)} hosts?\n\n" + "\n".join(commands[:8])
            + ("\n…" if len(commands) > 8 else ""),
        ):
            return

        self._reset_results(targets)
        self.cancel = threading.Event()
        self.run_btn.configure(state="disabled")
        self.test_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.started_at = time.monotonic()
        self.summary.configure(
            text=f"{'Testing login on' if dry_run else 'Running on'} {len(targets)} host(s)…"
        )
        password = self.password.get()
        use_shell = self.use_shell.get()
        precheck = self.precheck.get()
        separate = self.separate.get()
        stop_on_error = self.stop_on_error.get()
        cancel = self.cancel
        verifier = (
            core.HostKeyVerifier(
                core.default_known_hosts_path(), self._ask_host_key, self.trust_known_keys.get()
            )
            if self.verify_keys.get()
            else None
        )

        def work():
            try:
                core.run_all(
                    targets, username, password, command, connect_t, command_t,
                    use_shell, workers, cancel,
                    on_result=lambda r: self.events.put(("result", r)),
                    verifier=verifier,
                    dry_run=dry_run,
                    precheck_timeout=precheck_t if precheck else None,
                    separate_commands=separate,
                    stop_on_error=stop_on_error,
                )
            finally:
                self.events.put(("done", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        for t in targets:
            self.events.put(("running", str(t)))  # shown as "Running" until a result arrives

    def _ask_host_key(self, question: core.HostKeyQuestion) -> str:
        """Called from a worker thread; the dialog itself runs on the GUI thread."""
        holder = {"event": threading.Event(), "answer": core.REJECT}
        self.events.put(("hostkey", (question, holder)))
        while not holder["event"].wait(0.2):
            if self.cancel.is_set():
                return core.REJECT
        return holder["answer"]

    def _hostkey_dialog(self, q: core.HostKeyQuestion) -> str:
        win = tk.Toplevel(self.root)
        win.title("Host key changed!" if q.changed else "Accept SSH host key?")
        win.minsize(640, 0)
        win.transient(self.root)
        win.resizable(True, False)
        answer = {"value": core.REJECT}
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)

        if q.changed:
            ttk.Label(
                frame,
                text="WARNING: the host key for this device has CHANGED",
                foreground="#b91c1c", font=("TkDefaultFont", 11, "bold"),
            ).pack(anchor="w")
            intro = (
                "It is different from the key you accepted before. This can mean someone "
                "is intercepting the connection. Only replace it if you know the device "
                "was reinstalled or its key was regenerated."
            )
        elif q.same_key_hosts:
            ttk.Label(
                frame, text="Known key, new address",
                font=("TkDefaultFont", 11, "bold"),
            ).pack(anchor="w")
            intro = (
                "This exact key is already accepted for another address, so this is "
                "probably the same device with a new IP:\n  "
                + "\n  ".join(q.same_key_hosts)
                + "\nIf it is a different machine (e.g. a cloned VM), reject it."
            )
        else:
            ttk.Label(
                frame, text="First connection to this device",
                font=("TkDefaultFont", 11, "bold"),
            ).pack(anchor="w")
            intro = (
                "The authenticity of this host can't be established yet. Compare the "
                "fingerprint with the one on the device if you can."
            )
        ttk.Label(frame, text=intro, wraplength=520, justify="left").pack(anchor="w", pady=(4, 10))

        grid = ttk.Frame(frame)
        grid.pack(fill="x")
        rows = [("Host", str(q.target)), ("Key type", q.key_type), ("Fingerprint", q.fingerprint)]
        if q.changed:
            rows.append(("Previous fingerprint", q.old_fingerprint))
        for i, (label, value) in enumerate(rows):
            ttk.Label(grid, text=label + ":").grid(row=i, column=0, sticky="nw", pady=2)
            e = ttk.Entry(grid, width=62)
            e.insert(0, value)
            e.configure(state="readonly")
            e.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=2)
        grid.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Public key:").pack(anchor="w", pady=(10, 2))
        key_box = tk.Text(frame, height=4, width=70, wrap="char", font="TkFixedFont")
        key_box.insert("1.0", q.public_key)
        key_box.configure(state="disabled")
        key_box.pack(fill="x")

        def choose(value):
            answer["value"] = value
            win.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(14, 0))
        reject = ttk.Button(buttons, text="Reject", command=lambda: choose(core.REJECT))
        if q.changed:
            ttk.Button(
                buttons, text="Replace key and connect", command=lambda: choose(core.ACCEPT)
            ).pack(side="right")
            reject.pack(side="right", padx=(0, 6))
        else:
            ttk.Button(
                buttons, text="Accept & remember", command=lambda: choose(core.ACCEPT)
            ).pack(side="right")
            if q.same_key_hosts:
                ttk.Button(
                    buttons, text="Accept & forget old address",
                    command=lambda: choose(core.ACCEPT_MOVE),
                ).pack(side="right", padx=(0, 6))
            ttk.Button(
                buttons, text="Accept all new hosts in this run",
                command=lambda: choose(core.ACCEPT_ALL),
            ).pack(side="right", padx=(0, 6))
            reject.pack(side="right", padx=(0, 6))
        reject.focus_set()
        win.protocol("WM_DELETE_WINDOW", lambda: choose(core.REJECT))

        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_height()) // 3
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.wait_window()
        return answer["value"]

    def show_known_hosts(self):
        path = core.default_known_hosts_path()
        win = tk.Toplevel(self.root)
        win.title("Accepted SSH host keys")
        win.transient(self.root)
        win.geometry("900x420")
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Stored in {path}", foreground="#6b7280").pack(anchor="w")
        cols = ("type", "fingerprint")
        tree = ttk.Treeview(frame, columns=cols, show="tree headings", height=8)
        tree.heading("#0", text="Host")
        tree.heading("type", text="Key type")
        tree.heading("fingerprint", text="Fingerprint")
        tree.column("#0", width=200)
        tree.column("type", width=110)
        tree.column("fingerprint", width=420)
        tree.pack(fill="both", expand=True, pady=6)
        ttk.Label(frame, text="Public key:").pack(anchor="w")
        box = tk.Text(frame, height=4, wrap="char", font="TkFixedFont", state="disabled")
        box.pack(fill="x")
        keys: dict[str, tuple[str, str]] = {}

        def reload():
            tree.delete(*tree.get_children())
            keys.clear()
            for i, (name, ktype, fp, line) in enumerate(core.list_known_hosts(path)):
                iid = str(i)
                keys[iid] = (name, line)
                tree.insert("", "end", iid=iid, text=name, values=(ktype, fp))
            show()

        def show(_e=None):
            sel = tree.selection()
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", keys[sel[0]][1] if sel else "Select a host to see its public key.")
            box.configure(state="disabled")

        def remove():
            sel = tree.selection()
            if not sel:
                return
            name = keys[sel[0]][0]
            if messagebox.askokcancel(
                APP_TITLE, f"Forget the key for {name}?\nYou will be asked to accept it again next time.",
                parent=win,
            ):
                core.remove_known_host(path, name)
                reload()

        tree.bind("<<TreeviewSelect>>", show)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Close", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Forget selected", command=remove).pack(side="right", padx=6)
        reload()

    def stop(self):
        self.cancel.set()
        self.stop_btn.configure(state="disabled")
        self.summary.configure(text="Stopping…")

    def _reset_results(self, targets):
        self.tree.delete(*self.tree.get_children())
        self.results.clear()
        self.running.clear()
        self.order = [str(t) for t in targets]
        self._update_progress()
        self.tree.insert("", "end", iid=ALL_ROW, text="(all hosts)", values=("", "", ""))
        for key in self.order:
            self.tree.insert("", "end", iid=key, text=key, values=(STATUS_LABELS["Pending"], "", ""), tags=("Pending",))
        self.tree.selection_set(ALL_ROW)
        self._show_selected()

    def _poll(self):
        refresh = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "running":
                    if payload not in self.results and self.tree.exists(payload):
                        self.running.add(payload)
                        self._update_progress()
                        self.tree.item(payload, values=(STATUS_LABELS["Running"], "", ""), tags=("Running",))
                elif kind == "hostkey":
                    question, holder = payload
                    holder["answer"] = self._hostkey_dialog(question)
                    holder["event"].set()
                elif kind == "result":
                    self._apply_result(payload)
                    refresh = True
                elif kind == "done":
                    self._finish()
                    refresh = True
        except queue.Empty:
            pass
        if refresh:
            self._show_selected()
        if self.worker and self.worker.is_alive() and not self.cancel.is_set():
            self.summary.configure(
                text=f"{len(self.results)}/{len(self.order)} finished · "
                f"{time.monotonic() - self.started_at:.0f}s"
            )
        self.root.after(100, self._poll)

    def _update_progress(self):
        """Refresh the OK / Failed counters and the chunked progress bar."""
        ok = sum(1 for r in self.results.values() if r.status == core.OK)
        failed = sum(1 for r in self.results.values() if r.status in FAILED_STATUSES)
        self.ok_label.configure(text=f"OK: {ok}")
        self.failed_label.configure(text=f"Failed: {failed}")
        self._draw_bar()

    def _draw_bar(self):
        """One bar made of chunks that fill left to right as hosts finish.

        A chunk turns green when the hosts it covers are done (pass or fail;
        the OK / Failed counters and the rows show which); the next chunk is
        light green while hosts are running.
        """
        c = self.cubes
        c.delete("all")
        n = len(self.order)
        if not n:
            return
        width, height, gap = max(c.winfo_width(), 1), 18, 2
        chunks = max(1, min(n, (width + gap) // (8 + gap)))
        chunk_w = (width - gap * (chunks - 1)) / chunks
        statuses = [r.status for r in self.results.values()]  # completion order
        done = len(statuses)
        for i in range(chunks):
            first, last = i * n // chunks, (i + 1) * n // chunks  # hosts covered
            if last <= done and last > first:
                colour = CUBE_COLOURS[core.OK]
            elif self.running and first <= done < last:
                colour = CUBE_COLOURS["Running"]
            else:
                colour = CUBE_COLOURS["Pending"]
            x = i * (chunk_w + gap)
            c.create_rectangle(x, 0, x + chunk_w, height, width=0, fill=colour)

    def _apply_result(self, r: core.Result):
        key = str(r.target)
        self.results[key] = r
        if self.tree.exists(key):
            exit_text = "" if r.exit_status is None else str(r.exit_status)
            self.tree.item(
                key,
                values=(STATUS_LABELS.get(r.status, r.status), exit_text, f"{r.seconds:.1f}s"),
                tags=(r.status,),
            )
        done = len(self.results)
        self.running.discard(key)
        self._update_progress()
        self.summary.configure(
            text=f"{done}/{len(self.order)} finished · {time.monotonic() - self.started_at:.0f}s"
        )

    def _finish(self):
        for key in self.order:  # anything never reached (stopped early)
            if key not in self.results and self.tree.exists(key):
                self.tree.item(key, values=(STATUS_LABELS[core.CANCELLED], "", ""), tags=(core.CANCELLED,))
        counts: dict[str, int] = {}
        for r in self.results.values():
            counts[r.status] = counts.get(r.status, 0) + 1
        parts = [f"{n} {STATUS_LABELS.get(s, s).lstrip('✔✖ ')}" for s, n in counts.items()]
        self.summary.configure(text="Done: " + (", ".join(parts) or "nothing ran"))
        self.running.clear()
        self._update_progress()
        self.run_btn.configure(state="normal")
        self.test_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    # ------------------------------------------------------------ output
    @staticmethod
    def _format_result(r: core.Result) -> str:
        head = f"===== {r.target}  [{STATUS_LABELS.get(r.status, r.status)}]"
        if r.exit_status is not None:
            head += f"  exit={r.exit_status}"
        head += f"  {r.seconds:.1f}s ====="
        body = r.output.rstrip("\n")
        err = r.error.rstrip("\n")
        parts = [head]
        if body:
            parts.append(body)
        if err:
            parts.append(("\n--- stderr / error ---\n" if body else "") + err)
        return "\n".join(parts)

    def _log_text(self, keys: list[str]) -> str:
        chunks = []
        for key in keys:
            r = self.results.get(key)
            if r is None:
                chunks.append(f"===== {key}  [pending] =====")
            else:
                chunks.append(self._format_result(r))
        return "\n\n".join(chunks)

    def _selected_keys(self) -> list[str]:
        sel = [s for s in self.tree.selection()]
        if not sel or ALL_ROW in sel:
            return list(self.order)
        return [k for k in self.order if k in sel]

    def _show_selected(self):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        for n, key in enumerate(self._selected_keys()):
            r = self.results.get(key)
            if n:
                self.output.insert("end", "\n\n")
            if r is None:
                self.output.insert("end", f"===== {key}  [pending] =====", "Pending")
                continue
            head, _, rest = self._format_result(r).partition("\n")
            self.output.insert("end", head, r.status)  # green = done, red = failed
            for line in rest.split("\n") if rest else []:
                self.output.insert("end", "\n")
                self.output.insert("end", line, "cmd" if line.startswith("$ ") else "")
        self.output.configure(state="disabled")

    def copy_output(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.output.get("1.0", "end").rstrip("\n"))

    def save_logs(self):
        if not self.order:
            messagebox.showinfo(APP_TITLE, "There are no results to save yet.")
            return
        default = f"ssh-logs-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path = filedialog.asksaveasfilename(
            title="Save logs", defaultextension=".txt", initialfile=default,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        header = (
            f"MultiNode SSH Console - {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Command:\n{self.command_text.get('1.0', 'end').strip()}\n\n"
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + self._log_text(self.order) + "\n")
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Could not save file:\n{e}")


def main():
    root = tk.Tk()
    root.title(f"{APP_TITLE} v{VERSION}")
    App(root)
    # Size the window from what the layout needs so the bottom buttons are
    # never cut off, but never larger than the screen.
    root.update_idletasks()
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    need_w, need_h = root.winfo_reqwidth(), root.winfo_reqheight()
    width = min(max(1350, need_w), screen_w - 40)
    height = min(max(950, need_h + 40), screen_h - 80)
    root.geometry(f"{width}x{height}+{max((screen_w - width) // 2, 0)}+{max((screen_h - height) // 3, 0)}")
    root.minsize(min(1000, width), min(need_h, height))
    root.mainloop()


if __name__ == "__main__":
    main()
