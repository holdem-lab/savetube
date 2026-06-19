#!/usr/bin/env python3
"""SaveTube — simple queue downloader for YouTube (and any yt-dlp site).

Paste links, build a queue, press Download. Items download one by one.
A broken link is marked failed and skipped; the queue keeps going.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path

# GUI apps launched from Finder inherit a minimal PATH without Homebrew, so
# yt-dlp/ffmpeg (in /opt/homebrew/bin or /usr/local/bin) would be invisible.
# Without ffmpeg, yt-dlp can't merge video+audio → only the audio survives.
for _d in ("/opt/homebrew/bin", "/usr/local/bin"):
    if _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

YT_DLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg")  # absolute path or None

ERROR_LOG = Path.home() / ".savetube" / "last_error.log"
# Max video height per label; None = best available.
RES_MAP = {"Лучшее": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

CONFIG_PATH = Path.home() / ".savetube" / "config.json"
PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
SPEED_RE = re.compile(r"\bat\s+([0-9.]+\s?[KMG]?i?B/s)")


def load_config() -> dict:
    # Remember the last folder/quality so the user sets them once.
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass  # config is a nicety, never block the app on it


class SaveTube:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        # Each queue row: {"url": str, "status": str, "iid": tree-item-id}
        self.items: list[dict] = []
        self.downloading = False

        root.title("SaveTube")
        root.geometry("640x560")
        root.minsize(520, 460)

        self._build_input()
        self._build_options()
        self._build_queue()
        self._build_controls()

        if not FFMPEG:  # video+audio merge and mp3 extraction both need it
            messagebox.showwarning(
                "SaveTube",
                "ffmpeg не найден — видео может скачаться без звука, mp3 не "
                "сработает.\nУстанови: brew install ffmpeg")

    # --- UI sections -------------------------------------------------
    def _build_input(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Ссылки (по одной на строку)")
        frame.pack(fill="x", padx=10, pady=(10, 4))

        self.input = tk.Text(frame, height=4, wrap="none")
        self.input.pack(fill="x", padx=8, pady=8)
        self._enable_clipboard(self.input)

        ttk.Button(frame, text="＋ Добавить в очередь",
                   command=self.add_to_queue).pack(anchor="e", padx=8, pady=(0, 8))

    def _enable_clipboard(self, widget: tk.Text) -> None:
        # Tk on macOS binds Cmd+C/V/X to Latin keysyms, so a Russian layout
        # breaks paste. Bind by physical mac keycode (layout-independent) and
        # add a right-click menu as a reliable fallback.
        def on_cmd(event):
            actions = {9: "<<Paste>>", 8: "<<Copy>>", 7: "<<Cut>>"}
            virtual = actions.get(event.keycode)
            if virtual:
                event.widget.event_generate(virtual)
                return "break"
            if event.keycode == 0:  # 'a' — select all
                event.widget.tag_add("sel", "1.0", "end")
                return "break"
        widget.bind("<Command-KeyPress>", on_cmd)

        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Вставить",
                         command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_command(label="Копировать",
                         command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вырезать",
                         command=lambda: widget.event_generate("<<Cut>>"))

        def popup(event):
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind("<Button-2>", popup)      # mac right-click
        widget.bind("<Control-Button-1>", popup)

    def _build_options(self) -> None:
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=4)

        # Always start on video so a one-off mp3 run never silently sticks.
        self.quality = tk.StringVar(value="video")
        ttk.Label(frame, text="Формат:").pack(side="left")
        ttk.Radiobutton(frame, text="Видео mp4", value="video",
                        variable=self.quality).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(frame, text="Звук mp3", value="audio",
                        variable=self.quality).pack(side="left", padx=(6, 0))

        ttk.Label(frame, text="Качество:").pack(side="left", padx=(14, 0))
        self.res = tk.StringVar(value="Лучшее")
        ttk.Combobox(frame, textvariable=self.res, width=8, state="readonly",
                     values=list(RES_MAP)).pack(side="left", padx=(6, 0))

        default_dir = self.cfg.get("folder", str(Path.home() / "Downloads"))
        self.folder = tk.StringVar(value=default_dir)
        ttk.Button(frame, text="Папка…",
                   command=self.pick_folder).pack(side="right")
        self.folder_label = ttk.Label(frame, textvariable=self.folder,
                                       foreground="#555")
        self.folder_label.pack(side="right", padx=(0, 8))

    def _build_queue(self) -> None:
        frame = ttk.LabelFrame(self.root, text="Очередь")
        frame.pack(fill="both", expand=True, padx=10, pady=4)

        cols = ("status", "url")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings",
                                 selectmode="browse")
        self.tree.heading("status", text="Статус")
        self.tree.heading("url", text="Ссылка")
        self.tree.column("status", width=110, anchor="w", stretch=False)
        self.tree.column("url", width=480, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        sb.pack(side="right", fill="y", pady=8)
        self.tree.configure(yscrollcommand=sb.set)
        # Double-click a failed row to read why it failed.
        self.tree.bind("<Double-1>", self.show_error)

    def _build_controls(self) -> None:
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=(4, 10))

        self.remove_btn = ttk.Button(frame, text="Удалить выбранное",
                                     command=self.remove_selected)
        self.remove_btn.pack(side="left")
        ttk.Button(frame, text="Очистить",
                   command=self.clear_queue).pack(side="left", padx=(6, 0))

        self.download_btn = ttk.Button(frame, text="▶ Скачать всё",
                                       command=self.start_download)
        self.download_btn.pack(side="right")

        self.status_var = tk.StringVar(value="Готов")
        ttk.Label(self.root, textvariable=self.status_var,
                  foreground="#555").pack(anchor="w", padx=12, pady=(0, 8))

    # --- queue ops ---------------------------------------------------
    def add_to_queue(self) -> None:
        raw = self.input.get("1.0", "end").strip()
        urls = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        added = 0
        for url in urls:
            iid = self.tree.insert("", "end", values=("⏳ в очереди", url))
            self.items.append({"url": url, "status": "queued", "iid": iid})
            added += 1
        if added:
            self.input.delete("1.0", "end")
            self.status_var.set(f"Добавлено: {added}")

    def remove_selected(self) -> None:
        if self.downloading:
            return
        for iid in self.tree.selection():
            self.items = [it for it in self.items if it["iid"] != iid]
            self.tree.delete(iid)

    def clear_queue(self) -> None:
        if self.downloading:
            return
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.status_var.set("Готов")

    def pick_folder(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.folder.get())
        if chosen:
            self.folder.set(chosen)

    def _set_status(self, item: dict, label: str) -> None:
        # UI updates must run on the main thread (called via root.after).
        self.tree.set(item["iid"], "status", label)

    def show_error(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        item = next((it for it in self.items if it["iid"] == iid), None)
        if item and item.get("error"):
            messagebox.showerror("Ошибка yt-dlp", item["error"])

    # --- download engine --------------------------------------------
    def start_download(self) -> None:
        if self.downloading:
            return
        pending = [it for it in self.items if it["status"] in ("queued", "error")]
        if not pending:
            messagebox.showinfo("SaveTube", "Очередь пуста.")
            return

        # Persist folder only; quality always resets to video on launch.
        self.cfg["folder"] = self.folder.get()
        save_config(self.cfg)

        self.downloading = True
        self.download_btn.config(state="disabled")
        self.remove_btn.config(state="disabled")
        threading.Thread(target=self._run_queue, args=(pending,),
                         daemon=True).start()

    def _run_queue(self, pending: list[dict]) -> None:
        folder = self.folder.get()
        quality = self.quality.get()
        height = RES_MAP.get(self.res.get())
        done = 0
        for item in pending:
            item["status"] = "downloading"
            self.root.after(0, self._set_status, item, "⬇ качаю…")
            self.root.after(0, self.status_var.set,
                            f"Качаю {done + 1}/{len(pending)}…")
            ok = self._download_one(item, folder, quality, height)
            if ok:
                item["status"] = "done"
                self.root.after(0, self._set_status, item, "✅ готово")
                done += 1
            else:
                item["status"] = "error"
                self.root.after(0, self._set_status, item, "❌ ошибка · 2× клик")
        self.root.after(0, self._finish, done, len(pending))

    def _download_one(self, item: dict, folder: str, quality: str,
                      height: int | None = None) -> bool:
        # [id] guarantees a unique ASCII name even if the title strips to
        # nothing; --restrict-filenames keeps names ASCII-only / no specials.
        out_tmpl = os.path.join(folder, "%(title)s_[%(id)s].%(ext)s")
        cmd = [YT_DLP, "--newline", "--restrict-filenames", "-o", out_tmpl]
        if FFMPEG:  # let yt-dlp merge even when PATH is bare (Finder launch)
            cmd += ["--ffmpeg-location", FFMPEG]
        if quality == "audio":
            cmd += ["-x", "--audio-format", "mp3"]
        elif height:  # cap to chosen resolution, fall back if not available
            fmt = (f"bv*[height<={height}]+ba/b[height<={height}]/"
                   f"bv*+ba/b")
            cmd += ["-f", fmt, "-S", "ext:mp4:m4a",
                    "--merge-output-format", "mp4"]
        else:
            cmd += ["-f", "bv*+ba/b", "-S", "ext:mp4:m4a",
                    "--merge-output-format", "mp4"]
        cmd.append(item["url"])

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)
        except FileNotFoundError:
            self.root.after(0, messagebox.showerror, "SaveTube",
                            "yt-dlp не найден. Переустанови (см. README).")
            return False

        tail = deque(maxlen=25)  # keep last lines to explain a failure
        for line in proc.stdout:  # live progress from yt-dlp
            tail.append(line.rstrip())
            m = PROGRESS_RE.search(line)
            if m:
                label = f"⬇ {m.group(1)}%"
                spd = SPEED_RE.search(line)
                if spd:  # speed missing on some early lines — show when present
                    label += f" · {spd.group(1).replace(' ', '')}"
                self.root.after(0, self._set_status, item, label)
        proc.wait()
        if proc.returncode != 0:
            self._record_error(item, tail)
            return False
        return True

    def _record_error(self, item: dict, tail) -> None:
        # ERROR lines first (the real reason), then the raw tail as fallback.
        errs = [ln for ln in tail if "ERROR" in ln or "error" in ln]
        reason = "\n".join(errs) if errs else "\n".join(tail)
        item["error"] = f"{item['url']}\n\n{reason}"
        try:
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with ERROR_LOG.open("a") as f:
                f.write(item["error"] + "\n" + "=" * 60 + "\n")
        except Exception:
            pass

    def _finish(self, done: int, total: int) -> None:
        self.downloading = False
        self.download_btn.config(state="normal")
        self.remove_btn.config(state="normal")
        failed = total - done
        msg = f"Готово: {done}/{total}"
        if failed:
            msg += f" · ошибок: {failed}"
        self.status_var.set(msg)


def main() -> None:
    root = tk.Tk()
    SaveTube(root)
    root.mainloop()


if __name__ == "__main__":
    main()
