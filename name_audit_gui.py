#!/usr/bin/env python3
"""
name_audit_gui.py

A minimal desktop front end for presidio_name_audit.py.

Handles two things a Command-Prompt workflow otherwise requires:
  1. One-time install of the non-Python dependencies (pip packages +
     the spaCy language model).
  2. Running "scan" + "report" against a folder of OCR'd PDFs, and
     clearing the cache, without typing anything.

This file must live in the same folder as presidio_name_audit.py --
it calls that script as a subprocess, the same way you would from
Command Prompt, and simply streams its output into a log box.

No dependencies beyond the Python standard library (tkinter, which
ships with Python on Windows).
"""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET_SCRIPT = SCRIPT_DIR / "presidio_name_audit.py"

REQUIRED_PACKAGES = ["presidio_analyzer", "pdfplumber", "spacy", "rapidfuzz", "numpy"]
SPACY_MODEL = "en_core_web_lg"

FONT = ("Segoe UI", 10)
MONO_FONT = ("Consolas", 9)


# ---------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------
def missing_dependencies():
    """Return a list of human-readable names for whatever isn't installed yet."""
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if "spacy" not in missing:
        try:
            import spacy
            if not spacy.util.is_package(SPACY_MODEL):
                missing.append(SPACY_MODEL)
        except Exception:
            missing.append(SPACY_MODEL)
    else:
        missing.append(SPACY_MODEL)

    return missing


# ---------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------
class NameAuditApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Name Audit")
        self.geometry("640x520")
        self.minsize(560, 420)
        self.configure(bg="#fafafa")

        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=FONT, background="#fafafa")
        style.configure("TButton", padding=6)
        style.configure("Accent.TButton", padding=8)

        self.pdf_folder = tk.StringVar()
        self.out_csv = tk.StringVar()
        self.cache_dir = tk.StringVar(value=".name_audit_cache")
        self.status = tk.StringVar(value="Ready.")

        self._log_queue = queue.Queue()
        self._running = False

        self._build_layout()
        self.after(100, self._poll_log_queue)
        self.after(50, self._check_setup)

    # -- layout -----------------------------------------------------
    def _build_layout(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        # Setup notice (shown only if something is missing)
        self.setup_frame = ttk.Frame(outer)
        self.setup_label = ttk.Label(
            self.setup_frame, text="", foreground="#8a5a00", wraplength=560, justify="left"
        )
        self.setup_label.pack(anchor="w")
        self.setup_button = ttk.Button(
            self.setup_frame, text="Install Dependencies", command=self._install_dependencies
        )
        self.setup_button.pack(anchor="w", pady=(6, 0))
        ttk.Separator(self.setup_frame).pack(fill="x", pady=12)
        # setup_frame is packed/unpacked dynamically in _check_setup

        # PDF folder
        row1 = ttk.Frame(outer)
        row1.pack(fill="x", pady=(0, 8))
        self._first_row = row1
        ttk.Label(row1, text="PDF folder").pack(anchor="w")
        f1 = ttk.Frame(row1)
        f1.pack(fill="x", pady=(2, 0))
        ttk.Entry(f1, textvariable=self.pdf_folder).pack(side="left", fill="x", expand=True)
        ttk.Button(f1, text="Browse…", command=self._pick_pdf_folder).pack(side="left", padx=(6, 0))

        # Output CSV
        row2 = ttk.Frame(outer)
        row2.pack(fill="x", pady=(0, 8))
        ttk.Label(row2, text="Output CSV").pack(anchor="w")
        f2 = ttk.Frame(row2)
        f2.pack(fill="x", pady=(2, 0))
        ttk.Entry(f2, textvariable=self.out_csv).pack(side="left", fill="x", expand=True)
        ttk.Button(f2, text="Save as…", command=self._pick_out_csv).pack(side="left", padx=(6, 0))

        # Advanced (cache dir) - kept simple, single row
        row3 = ttk.Frame(outer)
        row3.pack(fill="x", pady=(0, 8))
        ttk.Label(row3, text="Cache folder (optional — separate projects should use different ones)").pack(anchor="w")
        ttk.Entry(row3, textvariable=self.cache_dir).pack(fill="x", pady=(2, 0))

        # Buttons
        row4 = ttk.Frame(outer)
        row4.pack(fill="x", pady=(8, 8))
        self.run_button = ttk.Button(
            row4, text="Scan && Report", style="Accent.TButton", command=self._run_all
        )
        self.run_button.pack(side="left")
        self.clear_button = ttk.Button(row4, text="Clear Cache", command=self._run_clear)
        self.clear_button.pack(side="left", padx=(8, 0))

        # Status line
        ttk.Label(outer, textvariable=self.status, foreground="#666").pack(anchor="w")

        # Log box
        log_frame = ttk.Frame(outer)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.log_box = tk.Text(
            log_frame, font=MONO_FONT, bg="#111", fg="#ddd", wrap="word",
            state="disabled", relief="flat", padx=8, pady=8,
        )
        scroll = ttk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # -- setup check --------------------------------------------------
    def _check_setup(self):
        self._log("Checking dependencies…")
        missing = missing_dependencies()
        if missing:
            self.setup_label.config(
                text="Missing: " + ", ".join(missing) + ". Install before scanning."
            )
            self.setup_frame.pack(fill="x", before=self._first_row)
            self._set_running_state(False, allow_run=False)
            self._log("Not all dependencies are installed yet.")
        else:
            self.setup_frame.pack_forget()
            self._set_running_state(False, allow_run=True)
            self._log("All dependencies present.")

    def _install_dependencies(self):
        if self._running:
            return
        self._set_running_state(True, allow_run=False)
        self.status.set("Installing dependencies… this can take a few minutes.")
        thread = threading.Thread(target=self._install_dependencies_worker, daemon=True)
        thread.start()

    def _install_dependencies_worker(self):
        pip_cmd = [
            sys.executable, "-m", "pip", "install",
            "presidio-analyzer", "pdfplumber", "spacy", "rapidfuzz", "numpy",
            "--break-system-packages",
        ]
        ok = self._stream_subprocess(pip_cmd)
        if ok:
            model_cmd = [sys.executable, "-m", "spacy", "download", SPACY_MODEL]
            ok = self._stream_subprocess(model_cmd)

        def finish():
            self._set_running_state(False, allow_run=True)
            if ok:
                self.status.set("Dependencies installed.")
                self._check_setup()
            else:
                self.status.set("Install failed — see log below.")

        self.after(0, finish)

    # -- pickers -----------------------------------------------------
    def _pick_pdf_folder(self):
        path = filedialog.askdirectory(title="Select folder of OCR'd PDFs")
        if path:
            self.pdf_folder.set(path)
            if not self.out_csv.get():
                self.out_csv.set(str(Path(path) / "names_report.csv"))

    def _pick_out_csv(self):
        path = filedialog.asksaveasfilename(
            title="Save report as", defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if path:
            self.out_csv.set(path)

    # -- actions -------------------------------------------------------
    def _run_all(self):
        if self._running:
            return
        folder = self.pdf_folder.get().strip()
        if not folder:
            messagebox.showwarning("Name Audit", "Choose a PDF folder first.")
            return
        if not Path(folder).is_dir():
            messagebox.showerror("Name Audit", f"Folder not found:\n{folder}")
            return
        out = self.out_csv.get().strip() or str(Path(folder) / "names_report.csv")
        cache = self.cache_dir.get().strip() or ".name_audit_cache"

        cmd = [
            sys.executable, str(TARGET_SCRIPT), "all", folder,
            "--out", out, "--cache-dir", cache,
        ]
        self._set_running_state(True, allow_run=False)
        self.status.set("Scanning… this can take a while for large batches.")
        threading.Thread(target=self._run_worker, args=(cmd, "Scan complete."), daemon=True).start()

    def _run_clear(self):
        if self._running:
            return
        cache = self.cache_dir.get().strip() or ".name_audit_cache"
        if not messagebox.askyesno("Clear Cache", f"Delete cached detections in '{cache}'?"):
            return
        cmd = [sys.executable, str(TARGET_SCRIPT), "clear", "--cache-dir", cache, "--yes"]
        self._set_running_state(True, allow_run=False)
        self.status.set("Clearing cache…")
        threading.Thread(target=self._run_worker, args=(cmd, "Cache cleared."), daemon=True).start()

    def _run_worker(self, cmd, done_message):
        ok = self._stream_subprocess(cmd)

        def finish():
            self._set_running_state(False, allow_run=True)
            self.status.set(done_message if ok else "Finished with errors — see log below.")

        self.after(0, finish)

    # -- subprocess + logging helpers ---------------------------------
    def _stream_subprocess(self, cmd):
        self._log("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(SCRIPT_DIR),
            )
        except FileNotFoundError as e:
            self._log(f"Could not start process: {e}")
            return False

        for line in proc.stdout:
            self._log(line.rstrip("\n"))
        proc.wait()
        self._log(f"(exit code {proc.returncode})")
        return proc.returncode == 0

    def _log(self, text):
        self._log_queue.put(text)

    def _poll_log_queue(self):
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(100, self._poll_log_queue)

    def _set_running_state(self, running, allow_run):
        self._running = running
        busy_state = "disabled" if running else "normal"
        self.run_button.config(state="normal" if (allow_run and not running) else "disabled")
        self.clear_button.config(state=busy_state)
        self.setup_button.config(state=busy_state)


if __name__ == "__main__":
    if not TARGET_SCRIPT.exists():
        tk.Tk().withdraw()
        messagebox.showerror(
            "Name Audit",
            f"Could not find presidio_name_audit.py in:\n{SCRIPT_DIR}\n\n"
            "Put this file in the same folder as the script.",
        )
        sys.exit(1)
    app = NameAuditApp()
    app.mainloop()
