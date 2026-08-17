from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import unicodedata
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent


def load_module(filename: str, module_name: str):
    path = BASE_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


watcher_module = load_module("download-watcher.py", "download_watcher")
automation_module = load_module("qq-automation.py", "qq_automation")

DownloadWatcher = watcher_module.DownloadWatcher
expand_path = watcher_module.expand_path
sanitize_filename = watcher_module.sanitize_filename
QQMusicAutomation = automation_module.QQMusicAutomation
AutomationError = automation_module.AutomationError
AutomationStopped = automation_module.AutomationStopped
SkipCurrent = automation_module.SkipCurrent


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("QQMusic Web Helper - Automated")
        self.geometry("1180x900")
        self.minsize(1020, 760)

        self.config_data = json.loads(
            (BASE_DIR / "config.json").read_text(encoding="utf-8")
        )
        self.task_file = expand_path(self.config_data["task_file"], BASE_DIR)
        self.downloaded_folder = expand_path(
            self.config_data["downloaded_folder"], BASE_DIR
        )
        self.log_folder = expand_path(self.config_data["log_folder"], BASE_DIR)
        self.watch_folder = expand_path(self.config_data["watch_folder"], BASE_DIR)
        self.log_folder.mkdir(parents=True, exist_ok=True)
        self.watch_folder.mkdir(parents=True, exist_ok=True)
        self.downloaded_folder.mkdir(parents=True, exist_ok=True)

        with self.task_file.open("r", encoding="utf-8-sig", newline="") as f:
            self.rows = list(csv.DictReader(f))
            self.fieldnames = list(self.rows[0].keys()) if self.rows else []

        if not self.rows:
            raise RuntimeError("Task CSV is empty.")

        self.startup_repairs = self._reconcile_done_from_output()
        if self.startup_repairs:
            self._save_rows()

        self.current_index = self._find_resume_index()

        self.watcher = DownloadWatcher(
            self.watch_folder,
            self.downloaded_folder,
            set(self.config_data.get("ignore_extensions", [])),
            float(self.config_data.get("poll_interval_seconds", 0.5)),
            float(self.config_data.get("stable_seconds", 2.0)),
        )

        self.stop_watch = False
        self.watch_thread = None

        self.auto_thread = None
        self.auto_stop = threading.Event()
        self.auto_pause = threading.Event()
        self.auto_skip = threading.Event()
        self.batch_mode = False

        self._build_ui()
        self._show_current()
        self._update_counts()
        if self.startup_repairs:
            for task_no, old_status, filename in self.startup_repairs:
                self.log(
                    f"Startup reconcile: task {task_no} {old_status} -> Done "
                    f"because output exists: {filename}"
                )
            self.log(
                f"Startup reconcile repaired {len(self.startup_repairs)} task(s)."
            )
        self.after(200, self._refresh_browser_status)

    @staticmethod
    def _filename_key(value: str) -> str:
        return unicodedata.normalize("NFC", value or "").casefold()

    @classmethod
    def _stem_matches_final_base(cls, stem: str, final_base: str) -> bool:
        stem_key = cls._filename_key(stem)
        base_key = cls._filename_key(final_base)

        if stem_key == base_key:
            return True

        prefix = base_key + " ("
        if stem_key.startswith(prefix) and stem_key.endswith(")"):
            number = stem_key[len(prefix):-1]
            return number.isdigit() and int(number) >= 2

        return False

    def _row_final_bases(self, row):
        # Resolved Artist / Resolved Title are legacy columns only.
        # Never use them as an input source.
        artist = row.get("Part A", "").strip()
        title = row.get("Part B", "").strip()

        if not artist or not title:
            return []

        return [sanitize_filename(f"{title} - {artist}")]

    def _reconcile_done_from_output(self):
        # One-way startup repair:
        # finalized output file exists -> stale CSV state becomes Done.
        # Existing Done rows are never demoted.
        # Skipped rows are untouched.
        # One file can satisfy only one task.
        ignored = {
            str(x).lower()
            for x in self.config_data.get("ignore_extensions", [])
        }
        audio_extensions = {
            ".m4a", ".mp3", ".flac", ".wav", ".aac",
            ".ogg", ".opus", ".wma", ".webm", ".mp4",
        }

        files = []
        if self.downloaded_folder.exists():
            for path in self.downloaded_folder.iterdir():
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix in ignored:
                    continue
                if suffix not in audio_extensions:
                    continue
                files.append(path)

        used = set()
        repairs = []

        def claim_matching_file(row):
            bases = self._row_final_bases(row)
            if not bases:
                return None

            for file_index, path in enumerate(files):
                if file_index in used:
                    continue
                for base in bases:
                    if self._stem_matches_final_base(path.stem, base):
                        used.add(file_index)
                        return path
            return None

        for row in self.rows:
            if row.get("Status", "Pending") == "Done":
                claim_matching_file(row)

        for index, row in enumerate(self.rows):
            status = row.get("Status", "Pending")
            if status in {"Done", "Skipped"}:
                continue

            matched_file = claim_matching_file(row)
            if matched_file is None:
                continue

            old_status = status
            row["Status"] = "Done"
            repairs.append((index + 1, old_status, matched_file.name))

        return repairs

    def _find_resume_index(self):
        for i, row in enumerate(self.rows):
            if row.get("Status", "Pending") not in {"Done", "Skipped"}:
                return i
        return max(0, len(self.rows) - 1)

    def _save_rows(self):
        with self.task_file.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.fieldnames)
            writer.writeheader()
            writer.writerows(self.rows)

    def current_row(self):
        return self.rows[self.current_index]

    def _build_ui(self):
        pad = 10

        top = ttk.Frame(self)
        top.pack(fill="x", padx=pad, pady=(pad, 4))

        self.progress_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.counts_var = tk.StringVar()

        ttk.Label(
            top, textvariable=self.progress_var, font=("Segoe UI", 13, "bold")
        ).pack(side="left")
        ttk.Label(top, textvariable=self.counts_var).pack(side="left", padx=18)
        ttk.Label(top, textvariable=self.status_var).pack(side="right")

        info = ttk.LabelFrame(self, text="Current task")
        info.pack(fill="x", padx=pad, pady=5)

        self.no_var = tk.StringVar()
        self.part_a_var = tk.StringVar()
        self.part_b_var = tk.StringVar()
        self.query_var = tk.StringVar()
        self.original_var = tk.StringVar()
        self.duplicate_var = tk.StringVar()

        fields = [
            ("No.", self.no_var),
            ("Part A", self.part_a_var),
            ("Part B", self.part_b_var),
            ("Search Query", self.query_var),
            ("Original Filename", self.original_var),
            ("Duplicate", self.duplicate_var),
        ]

        for r, (label, var) in enumerate(fields):
            ttk.Label(info, text=label, width=18).grid(
                row=r, column=0, sticky="nw", padx=8, pady=3
            )
            entry = ttk.Entry(info, textvariable=var)
            entry.grid(row=r, column=1, sticky="ew", padx=8, pady=3)
            if label != "Search Query":
                entry.state(["readonly"])

        info.columnconfigure(1, weight=1)

        ttk.Button(
            info, text="Copy Search Query", command=self.copy_query
        ).grid(row=len(fields), column=1, sticky="w", padx=8, pady=(3, 7))

        auto = ttk.LabelFrame(self, text="Automation")
        auto.pack(fill="x", padx=pad, pady=5)

        self.browser_step_var = tk.StringVar(value="Not checked")
        self.search_step_var = tk.StringVar(value="—")
        self.match_step_var = tk.StringVar(value="—")
        self.play_step_var = tk.StringVar(value="—")
        self.capture_step_var = tk.StringVar(value="—")
        self.download_step_var = tk.StringVar(value="—")
        self.finalize_step_var = tk.StringVar(value="—")

        step_rows = [
            ("Chrome / CDP", self.browser_step_var),
            ("Search", self.search_step_var),
            ("Best match", self.match_step_var),
            ("Playback", self.play_step_var),
            ("Cat Catch", self.capture_step_var),
            ("Direct download", self.download_step_var),
            ("Rename / finalize", self.finalize_step_var),
        ]

        for r, (label, var) in enumerate(step_rows):
            ttk.Label(auto, text=label, width=20).grid(
                row=r, column=0, sticky="w", padx=8, pady=2
            )
            ttk.Label(auto, textvariable=var).grid(
                row=r, column=1, sticky="w", padx=8, pady=2
            )

        controls = ttk.Frame(auto)
        controls.grid(row=0, column=2, rowspan=7, sticky="ne", padx=8, pady=5)

        self.start_current_btn = ttk.Button(
            controls, text="Start current", command=self.start_current
        )
        self.start_current_btn.grid(row=0, column=0, padx=4, pady=3)

        self.start_batch_btn = ttk.Button(
            controls, text="Start batch", command=self.start_batch
        )
        self.start_batch_btn.grid(row=0, column=1, padx=4, pady=3)

        self.pause_btn = ttk.Button(
            controls, text="Pause", command=self.pause_automation
        )
        self.pause_btn.grid(row=1, column=0, padx=4, pady=3)

        self.resume_btn = ttk.Button(
            controls, text="Resume", command=self.resume_automation
        )
        self.resume_btn.grid(row=1, column=1, padx=4, pady=3)

        self.skip_auto_btn = ttk.Button(
            controls, text="Skip current", command=self.skip_current_auto
        )
        self.skip_auto_btn.grid(row=2, column=0, padx=4, pady=3)

        self.stop_btn = ttk.Button(
            controls, text="Stop", command=self.stop_automation
        )
        self.stop_btn.grid(row=2, column=1, padx=4, pady=3)

        ttk.Button(
            controls, text="Check / start Chrome", command=self.ensure_chrome_async
        ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=3)

        auto.columnconfigure(1, weight=1)

        actions = ttk.LabelFrame(self, text="Manual fallback / navigation")
        actions.pack(fill="x", padx=pad, pady=5)

        self.arm_btn = ttk.Button(
            actions, text="Arm download watcher", command=self.arm_watcher
        )
        self.arm_btn.pack(side="left", padx=8, pady=7)

        ttk.Button(
            actions, text="Mark Review", command=lambda: self.set_status("Review")
        ).pack(side="left", padx=5)
        ttk.Button(
            actions, text="Skip", command=lambda: self.set_status("Skipped", True)
        ).pack(side="left", padx=5)
        ttk.Button(
            actions, text="Mark Done", command=lambda: self.set_status("Done", True)
        ).pack(side="left", padx=5)

        nav = ttk.Frame(actions)
        nav.pack(side="right", padx=8, pady=7)
        ttk.Button(nav, text="< Previous", command=self.previous).pack(
            side="left", padx=4
        )
        ttk.Button(nav, text="Next >", command=self.next).pack(
            side="left", padx=4
        )

        paths = ttk.LabelFrame(self, text="Folders")
        paths.pack(fill="x", padx=pad, pady=5)

        self.watch_path_var = tk.StringVar(value=str(self.watch_folder))
        self.output_path_var = tk.StringVar(value=str(self.downloaded_folder))

        ttk.Label(paths, text="Capture / working folder", width=22).grid(
            row=0, column=0, sticky="w", padx=8, pady=3
        )
        ttk.Entry(
            paths, textvariable=self.watch_path_var, state="readonly"
        ).grid(row=0, column=1, sticky="ew", padx=8, pady=3)

        ttk.Label(paths, text="Final output folder", width=22).grid(
            row=1, column=0, sticky="w", padx=8, pady=3
        )
        ttk.Entry(
            paths, textvariable=self.output_path_var, state="readonly"
        ).grid(row=1, column=1, sticky="ew", padx=8, pady=3)

        paths.columnconfigure(1, weight=1)

        logbox = ttk.LabelFrame(self, text="Session log")
        logbox.pack(fill="both", expand=True, padx=pad, pady=(5, pad))

        self.log_text = tk.Text(
            logbox, height=14, wrap="word", font=("Consolas", 10)
        )
        self.log_text.pack(fill="both", expand=True, padx=6, pady=6)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _show_current(self):
        row = self.current_row()
        self.progress_var.set(
            f"Task {self.current_index + 1} / {len(self.rows)}    "
            f"Status: {row.get('Status', 'Pending')}"
        )

        self.no_var.set(row.get("No.", ""))
        self.part_a_var.set(row.get("Part A", ""))
        self.part_b_var.set(row.get("Part B", ""))
        self.query_var.set(row.get("Search Query", ""))
        self.original_var.set(row.get("Original Filename", ""))
        self.duplicate_var.set(row.get("Duplicate", ""))

        if not self._automation_running():
            self.status_var.set("Ready")

        self.arm_btn.state(["!disabled"])
        self._reset_steps()
        self._update_counts()

    def _reset_steps(self):
        self.search_step_var.set("—")
        self.match_step_var.set("—")
        self.play_step_var.set("—")
        self.capture_step_var.set("—")
        self.download_step_var.set("—")
        self.finalize_step_var.set("—")

    def _update_counts(self):
        done = sum(r.get("Status") == "Done" for r in self.rows)
        skipped = sum(r.get("Status") == "Skipped" for r in self.rows)
        review = sum(r.get("Status") == "Review" for r in self.rows)
        remaining = len(self.rows) - done - skipped
        self.counts_var.set(
            f"Done {done}   Skipped {skipped}   Review {review}   Remaining {remaining}"
        )

    def log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        with (self.log_folder / "session.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _thread_log(self, text):
        self.after(0, lambda t=text: self.log(t))

    def _thread_step(self, name, value):
        def apply():
            mapping = {
                "browser": self.browser_step_var,
                "search": self.search_step_var,
                "match": self.match_step_var,
                "play": self.play_step_var,
                "capture": self.capture_step_var,
                "download": self.download_step_var,
            }
            var = mapping.get(name)
            if var is not None:
                var.set(value)
        self.after(0, apply)

    def copy_query(self):
        q = self.query_var.get().strip()
        self.clipboard_clear()
        self.clipboard_append(q)
        self.update()
        self.status_var.set("Search query copied")
        self.log(f"Copied query: {q}")

    def set_status(self, status, advance=False):
        row = self.current_row()
        row["Status"] = status
        self._save_rows()
        self.log(f"Task {self.current_index + 1} -> {status}")
        if advance:
            self.next()
        else:
            self._show_current()

    def previous(self):
        if self._automation_running():
            return
        self.cancel_watcher()
        if self.current_index > 0:
            self.current_index -= 1
            self._show_current()

    def next(self):
        if self._automation_running():
            return
        self.cancel_watcher()
        if self.current_index < len(self.rows) - 1:
            self.current_index += 1
            self._show_current()
        else:
            messagebox.showinfo("QQMusic Web Helper", "This is the last task.")

    def _automation_running(self):
        return self.auto_thread is not None and self.auto_thread.is_alive()

    def _cdp_alive(self):
        url = self.config_data.get("cdp_url", "http://127.0.0.1:9222").rstrip("/")
        try:
            with urllib.request.urlopen(url + "/json/version", timeout=1.0) as r:
                return r.status == 200
        except Exception:
            return False

    def _refresh_browser_status(self):
        if self._cdp_alive():
            self.browser_step_var.set("✓ CDP ready")
        else:
            self.browser_step_var.set("Not running")
        self.after(2000, self._refresh_browser_status)

    def _ensure_chrome(self):
        if self._cdp_alive():
            return True

        chrome = self.config_data.get(
            "chrome_path",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )
        profile = self.config_data.get(
            "chrome_profile",
            r"D:\QQMusic-Automation-Chrome",
        )
        port = int(self.config_data.get("cdp_port", 9222))

        Path(profile).mkdir(parents=True, exist_ok=True)

        subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 12
        while time.time() < deadline:
            if self._cdp_alive():
                return True
            time.sleep(0.5)
        return False

    def ensure_chrome_async(self):
        if self._automation_running():
            return

        def worker():
            self._thread_step("browser", "Starting...")
            ok = self._ensure_chrome()
            if ok:
                self._thread_step("browser", "✓ CDP ready")
                self._thread_log("Dedicated Chrome / CDP is ready.")
            else:
                self._thread_step("browser", "✗ Failed")
                self._thread_log("Could not start or reach dedicated Chrome.")

        threading.Thread(target=worker, daemon=True).start()

    def start_current(self):
        self._start_automation(batch=False)

    def start_batch(self):
        self._start_automation(batch=True)

    def _start_automation(self, batch):
        if self._automation_running():
            messagebox.showinfo("Automation", "Automation is already running.")
            return

        self.cancel_watcher()
        self.auto_stop.clear()
        self.auto_pause.clear()
        self.auto_skip.clear()
        self.batch_mode = batch

        self.status_var.set("Starting automation...")
        self.auto_thread = threading.Thread(
            target=self._automation_worker,
            args=(batch,),
            daemon=True,
        )
        self.auto_thread.start()

    def pause_automation(self):
        if self._automation_running():
            self.auto_pause.set()
            self.status_var.set("Paused")
            self.log("Automation paused.")

    def resume_automation(self):
        if self._automation_running():
            self.auto_pause.clear()
            self.status_var.set("Running")
            self.log("Automation resumed.")

    def skip_current_auto(self):
        if self._automation_running():
            self.auto_skip.set()
            self.status_var.set("Skipping current task...")

    def stop_automation(self):
        if self._automation_running():
            self.auto_stop.set()
            self.status_var.set("Stopping...")
            self.log("Stop requested.")

    def _set_row_status_threadsafe(self, index, status):
        self.rows[index]["Status"] = status
        self._save_rows()
        self.after(0, self._update_counts)

    def _automation_worker(self, batch):
        session = None

        try:
            self._thread_step("browser", "Checking...")
            if not self._ensure_chrome():
                raise RuntimeError("Dedicated Chrome / CDP could not be started.")

            session = QQMusicAutomation(
                self.config_data,
                log=self._thread_log,
                step=self._thread_step,
                stop_requested=self.auto_stop.is_set,
                pause_requested=self.auto_pause.is_set,
                skip_requested=self.auto_skip.is_set,
            )
            session.start()

            while True:
                if self.auto_stop.is_set():
                    raise AutomationStopped("Stopped by user.")

                index = self.current_index
                row = self.rows[index]

                if row.get("Status") in {"Done", "Skipped"}:
                    if batch and index < len(self.rows) - 1:
                        self.current_index += 1
                        self.after(0, self._show_current)
                        continue
                    break

                self.auto_skip.clear()
                self.after(0, self._reset_steps)
                self.after(0, lambda: self.status_var.set("Running"))

                artist = row.get("Part A", "").strip()
                title = row.get("Part B", "").strip()
                query = row.get("Search Query", "").strip() or f"{artist} {title}"

                if not artist or not title:
                    raise RuntimeError("Current task has no usable artist/title.")

                self._set_row_status_threadsafe(index, "Searching")
                self._thread_log(
                    f'Task {index + 1}/{len(self.rows)}: "{title}" / "{artist}"'
                )

                try:
                    result = session.run_track(
                        target_title=title,
                        target_artist=artist,
                        search_query=query,
                        output_dir=self.watch_folder,
                    )
                except SkipCurrent:
                    self.rows[index]["Status"] = "Skipped"
                    self._save_rows()
                    self._thread_log(f"Task {index + 1} skipped.")
                    self.after(0, self._update_counts)

                    if batch and index < len(self.rows) - 1:
                        self.current_index += 1
                        self.after(0, self._show_current)
                        continue
                    break

                except Exception as exc:
                    # A single failed song must not stop an overnight batch.
                    # Mark it Review, save the error in the log, and continue
                    # to the next task. In Start current mode, still stop here.
                    self.rows[index]["Status"] = "Review"
                    self._save_rows()
                    self._thread_log(
                        f"ERROR on task {index + 1}: {exc} -> marked Review."
                    )
                    self.after(0, self._update_counts)

                    if batch and index < len(self.rows) - 1:
                        self.current_index += 1
                        self.after(0, self._show_current)
                        continue

                    self.after(
                        0,
                        lambda e=str(exc): self.status_var.set(f"Review: {e}")
                    )
                    break

                except Exception as exc:
                    # A single failed song must not stop an overnight batch.
                    # Mark it Review, save the error in the log, and continue
                    # to the next task. In Start current mode, still stop here.
                    self.rows[index]["Status"] = "Review"
                    self._save_rows()
                    self._thread_log(
                        f"ERROR on task {index + 1}: {exc} -> marked Review."
                    )
                    self.after(0, self._update_counts)

                    if batch and index < len(self.rows) - 1:
                        self.current_index += 1
                        self.after(0, self._show_current)
                        continue

                    self.after(
                        0,
                        lambda e=str(exc): self.status_var.set(f"Review: {e}")
                    )
                    break

                self.rows[index]["Status"] = "Finalizing"
                self._save_rows()

                self._thread_step("finalize", "Renaming...")
                final_base = f"{result.matched_title} - {result.matched_artist}"

                dst = self.watcher.move_and_rename(
                    result.raw_path,
                    final_base,
                )

                self.rows[index]["Status"] = "Done"
                self._save_rows()

                self._thread_step("finalize", f"✓ {dst.name}")
                self._thread_log(
                    f"Done: {result.resource_name} -> {dst.name} "
                    f"({result.actual_size} bytes)"
                )

                self.after(0, self._update_counts)

                if index < len(self.rows) - 1:
                    self.current_index += 1
                    self.after(0, self._show_current)

                if not batch:
                    break

                if self.current_index >= len(self.rows):
                    break

            self.after(0, lambda: self.status_var.set("Ready"))

        except AutomationStopped:
            self._thread_log("Automation stopped.")
            self.after(0, lambda: self.status_var.set("Stopped"))

        except Exception as exc:
            index = self.current_index
            if 0 <= index < len(self.rows):
                self.rows[index]["Status"] = "Review"
                self._save_rows()

            self._thread_log(f"ERROR: {exc}")
            self.after(0, self._update_counts)
            self.after(0, lambda e=str(exc): self.status_var.set(f"Error: {e}"))

        finally:
            if session is not None:
                session.close()
            self.auto_pause.clear()
            self.auto_skip.clear()
            self.auto_stop.clear()
            self.after(0, self._show_current)

    # ----- Existing manual watcher is kept as fallback -----

    def arm_watcher(self):
        row = self.current_row()
        artist = row.get("Part A", "").strip()
        title = row.get("Part B", "").strip()

        if not artist or not title:
            messagebox.showwarning(
                "Missing task data",
                "Part A / Part B are empty, so the watcher cannot determine the final filename.",
            )
            return

        if self.watch_thread and self.watch_thread.is_alive():
            messagebox.showinfo("Watcher", "The watcher is already armed.")
            return

        if not self.watch_folder.exists():
            messagebox.showerror(
                "Watcher", f"Watch folder does not exist:\n{self.watch_folder}"
            )
            return

        self.stop_watch = False
        armed_at = time.time()
        self.arm_btn.state(["disabled"])

        self.current_row()["Status"] = "WaitingDownload"
        self._save_rows()

        self.log(f"Watcher armed for: {artist} - {title}")
        self.status_var.set("Waiting for download...")

        task_index = self.current_index
        self.watch_thread = threading.Thread(
            target=self._watch_worker,
            args=(task_index, artist, title, armed_at),
            daemon=True,
        )
        self.watch_thread.start()

    def _watch_worker(self, task_index, artist, title, armed_at):
        def stop():
            return self.stop_watch or task_index != self.current_index

        def status(msg):
            self.after(0, lambda: self.status_var.set(msg))

        found = self.watcher.wait_for_new_file(armed_at, stop, status)
        if found is None or stop():
            return

        try:
            dst = self.watcher.move_and_rename(
                found,
                f"{title} - {artist}",
            )
        except Exception as exc:
            self.after(0, lambda: self._watch_failed(str(exc)))
            return

        self.after(
            0,
            lambda: self._watch_success(
                task_index,
                found.name,
                dst,
            ),
        )

    def _watch_failed(self, message):
        self.arm_btn.state(["!disabled"])
        self.current_row()["Status"] = "Review"
        self._save_rows()
        self.status_var.set("Download handling failed")
        self.log(f"Download handling failed: {message}")
        messagebox.showerror("Download handling failed", message)

    def _watch_success(self, task_index, old_name, dst):
        if task_index != self.current_index:
            return

        if self.config_data.get("auto_mark_done_after_download", True):
            self.current_row()["Status"] = "Done"

        self._save_rows()
        self.log(f"Downloaded: {old_name} -> {dst.name}")
        self.status_var.set(f"Saved as {dst.name}")

        if self.config_data.get("auto_advance_after_download", True):
            if self.current_index < len(self.rows) - 1:
                self.current_index += 1
                self._show_current()
        else:
            self._show_current()

    def cancel_watcher(self):
        self.stop_watch = True
        self.arm_btn.state(["!disabled"])

    def on_close(self):
        self.auto_stop.set()
        self.cancel_watcher()
        self._save_rows()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()

