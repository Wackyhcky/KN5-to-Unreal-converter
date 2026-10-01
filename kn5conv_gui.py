#!/usr/bin/env python3
"""Small desktop window for the KN5 -> Unreal converter (uses tkinter, bundled with Python)."""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ac2ue.convert import convert  # noqa: E402
from ac2ue.car import convert_car, is_car_folder  # noqa: E402

UE_SCRIPT = os.path.join(HERE, "unreal", "ue_import_track.py").replace("\\", "/")


class _QueueStream:
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(s)

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KN5 to Unreal")
        self.geometry("760x540")
        self.minsize(600, 420)
        self.q = queue.Queue()
        self.manifest = None

        self.var_in = tk.StringVar()
        self.var_out = tk.StringVar()
        self.var_ac = tk.StringVar()
        self.var_layouts = tk.StringVar()
        self.var_inactive = tk.BooleanVar(value=False)

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        def row(r, label, var, buttons):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky="ew", padx=6)
            bf = ttk.Frame(frm)
            bf.grid(row=r, column=2, sticky="e")
            for text, cmd in buttons:
                ttk.Button(bf, text=text, command=cmd).pack(side="left", padx=2)

        row(0, "Track/car folder or .kn5", self.var_in,
            [("Folder…", self.pick_in_dir), ("KN5…", self.pick_in_file)])
        row(1, "Output folder", self.var_out, [("Browse…", self.pick_out)])
        row(2, "AC install (optional)", self.var_ac, [("Browse…", self.pick_ac)])
        ttk.Label(frm, text="Layouts (optional)").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.var_layouts).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Label(frm, text="comma-separated, blank = all", foreground="#777").grid(row=3, column=2, sticky="w")
        ttk.Checkbutton(frm, text="Include inactive (hidden) meshes",
                        variable=self.var_inactive).grid(row=4, column=1, sticky="w", pady=4)

        bar = ttk.Frame(frm)
        bar.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 6))
        self.btn_convert = ttk.Button(bar, text="Convert", command=self.start)
        self.btn_convert.pack(side="left")
        self.btn_copy = ttk.Button(bar, text="Copy Unreal command", command=self.copy_cmd, state="disabled")
        self.btn_copy.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        self.log = tk.Text(frm, height=16, wrap="word", font=("Consolas", 9))
        self.log.grid(row=6, column=0, columnspan=3, sticky="nsew")
        frm.rowconfigure(6, weight=1)
        self.after(100, self.drain)

    def pick_in_dir(self):
        d = filedialog.askdirectory(title="AC track or car folder (content/tracks|cars/<name>)")
        if d:
            self.var_in.set(d)
            if not self.var_out.get():
                self.var_out.set(os.path.join(os.path.expanduser("~"), "ac2ue", os.path.basename(d)))

    def pick_in_file(self):
        f = filedialog.askopenfilename(filetypes=[("KN5 model", "*.kn5")])
        if f:
            self.var_in.set(f)
            if not self.var_out.get():
                name = os.path.splitext(os.path.basename(f))[0]
                self.var_out.set(os.path.join(os.path.expanduser("~"), "ac2ue", name))

    def pick_out(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.var_out.set(d)

    def pick_ac(self):
        d = filedialog.askdirectory(title="Assetto Corsa install folder")
        if d:
            self.var_ac.set(d)

    def start(self):
        src, out = self.var_in.get().strip(), self.var_out.get().strip()
        if not src or not out:
            messagebox.showwarning("KN5 to Unreal", "Choose a track and an output folder first.")
            return
        layouts = [l.strip() for l in self.var_layouts.get().split(",") if l.strip()] or None
        self.log.delete("1.0", "end")
        self.btn_convert.configure(state="disabled")
        self.btn_copy.configure(state="disabled")
        self.progress.start(12)

        def work():
            try:
                if is_car_folder(src):
                    self.manifest = convert_car(src, out, "all", os.cpu_count() or 4,
                                                False, _QueueStream(self.q))
                else:
                    self.manifest = convert(src, out, self.var_ac.get().strip() or None, layouts,
                                            self.var_inactive.get(), os.cpu_count() or 4,
                                            False, _QueueStream(self.q))
                self.q.put(("done", None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple):
                    kind, msg = item
                    self.progress.stop()
                    self.btn_convert.configure(state="normal")
                    if kind == "done":
                        self.btn_copy.configure(state="normal")
                        self.log.insert("end", "\nNext: click 'Copy Unreal command', then paste it into "
                                               "Unreal's Output Log (Cmd box) and press Enter.\n")
                    else:
                        self.log.insert("end", f"\nERROR: {msg}\n")
                        messagebox.showerror("KN5 to Unreal", msg)
                else:
                    self.log.insert("end", item)
                self.log.see("end")
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def copy_cmd(self):
        if not self.manifest:
            return
        cmd = f'py "{UE_SCRIPT}" "{os.path.abspath(self.manifest).replace(os.sep, "/")}"'
        self.clipboard_clear()
        self.clipboard_append(cmd)
        self.log.insert("end", f"\nCopied: {cmd}\n")
        self.log.see("end")


if __name__ == "__main__":
    App().mainloop()
