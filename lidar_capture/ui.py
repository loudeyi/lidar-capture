"""Tkinter 界面。

只做两件事：把 Controller 的状态画出来，把按钮点击丢回 Controller 的队列。
所有判断逻辑都在 controller.py 里，这里不做任何“要不要算在录制”的推理。
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path

from .config import Config
from .controller import Controller
from .monitor import human_duration, human_rate, human_size
from .events import EventLog
from .transports import build_link

BG = "#eef1f7"
CARD = "#ffffff"
BORDER = "#dce3ef"
TEXT = "#1b2436"
MUTED = "#76839b"
FAINT = "#a3aec2"
ACCENT = "#2f6fed"
ACCENT_SOFT = "#e8eefb"
DARK = "#1b2436"
OK = "#15a34a"
WARN = "#d97706"
ERR = "#dc2626"
IDLE = "#9aa7bd"
CONSOLE_BG = "#101827"
CONSOLE_FG = "#c8d5ea"

STEP_ORDER = ("connect", "dmesg", "start", "stop")
STEP_TITLES = {
    "connect": "① 连接 3588",
    "dmesg": "② 静音内核日志",
    "start": "③ 开始录制",
    "stop": "④ 停止录制",
}
STATE_LABEL = {
    "idle": ("未开始", IDLE),
    "connecting": ("连接中", ACCENT),
    "ready": ("已连接", OK),
    "dmesg_ok": ("已就绪", OK),
    "starting": ("等 pcap 上包", WARN),
    "recording": ("录制中", OK),
    "stopping": ("停止中", WARN),
    "stopped": ("已停止", IDLE),
    "stop_failed": ("停止失败", ERR),
    "disconnected": ("连接断开", ERR),
    "error": ("出错", ERR),
}
STEP_STYLE = {
    "pending": ("等待", "#eef1f7", MUTED),
    "run": ("进行中", ACCENT_SOFT, ACCENT),
    "ok": ("完成", "#e4f7ec", OK),
    "warn": ("注意", "#fdf1dc", WARN),
    "fail": ("失败", "#fdeaea", ERR),
}


def pick_font(candidates, fallback="TkDefaultFont"):
    families = set(tkfont.families())
    for name in candidates:
        if name in families:
            return name
    return fallback


class StepCard(tk.Frame):
    """一行一步：序号、标题、实际下发的命令、状态胶囊。"""

    def __init__(self, master, title, command, ui_font, mono_font, index=1):
        super().__init__(master, bg=CARD)

        self.badge = tk.Canvas(self, width=24, height=24, bg=CARD, highlightthickness=0)
        self.badge.grid(row=0, column=0, rowspan=2, padx=(0, 10), pady=(1, 0), sticky="n")
        self.badge_id = self.badge.create_oval(1, 1, 23, 23, fill="#eef1f7", outline="")
        self.badge_text = self.badge.create_text(
            12, 12, text=str(index), fill=MUTED, font=(ui_font, 9, "bold")
        )

        head = tk.Frame(self, bg=CARD)
        head.grid(row=0, column=1, sticky="w")
        self.title = tk.Label(head, text=title, bg=CARD, fg=TEXT, font=(ui_font, 11, "bold"), anchor="w")
        self.title.pack(side="left")
        self.cmd = tk.Label(head, text=command, bg=CARD, fg=FAINT, font=(mono_font, 9), anchor="w")
        self.cmd.pack(side="left", padx=(12, 0))

        self.chip = tk.Label(self, text="等待", bg="#eef1f7", fg=MUTED, font=(ui_font, 9), padx=10, pady=1)
        self.chip.grid(row=0, column=2, sticky="e")

        self.detail = tk.Label(self, text="", bg=CARD, fg=MUTED, font=(ui_font, 9), anchor="w")
        self.detail.grid(row=1, column=1, columnspan=2, sticky="w", pady=(2, 0))

        self.columnconfigure(1, weight=1)
        self.columnconfigure(2, minsize=60)

    def set(self, status: str, detail: str = ""):
        label, bg, fg = STEP_STYLE.get(status, STEP_STYLE["pending"])
        self.chip.configure(text=label, bg=bg, fg=fg)
        self.badge.itemconfigure(self.badge_id, fill=bg)
        self.badge.itemconfigure(self.badge_text, fill=fg)
        self.detail.configure(text=detail)


class Sparkline(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, bg=CARD, highlightthickness=0, height=48, **kw)
        self.points: list[float] = []
        self.bind("<Configure>", lambda _e: self.redraw())

    def push(self, size: int | None):
        if size is not None:
            self.points.append(float(size))
            del self.points[:-160]
        self.redraw()

    def redraw(self):
        self.delete("all")
        w = max(self.winfo_width(), 60)
        h = max(self.winfo_height(), 30)
        for i in range(1, 3):
            y = h * i / 3
            self.create_line(0, y, w, y, fill="#eef1f7")
        pts = self.points[-120:]
        if len(pts) < 2:
            self.create_text(w / 2, h / 2, text="等待采样…", fill=FAINT, font=("TkDefaultFont", 9))
            return
        lo, hi = min(pts), max(pts)
        span = max(hi - lo, 1.0)
        step = w / max(len(pts) - 1, 1)
        coords = []
        for i, value in enumerate(pts):
            x = i * step
            y = h - 4 - (value - lo) / span * (h - 10)
            coords.extend((x, y))
        self.create_polygon(*coords, w, h, 0, h, fill="#dbe6fb", outline="")
        self.create_line(*coords, fill=ACCENT, width=2, smooth=True)
        self.create_oval(coords[-2] - 2.5, coords[-1] - 2.5, coords[-2] + 2.5, coords[-1] + 2.5, fill=ACCENT, outline="")


class App(tk.Tk):
    def __init__(self, cfg: Config, demo: bool = False):
        super().__init__()
        self.cfg = cfg
        self.demo = demo
        self.ui_font = pick_font(["Noto Sans CJK SC", "Source Han Sans SC", "DejaVu Sans"])
        self.mono_font = pick_font(["Noto Sans Mono CJK SC", "DejaVu Sans Mono", "Liberation Mono"], "TkFixedFont")

        self.title("LiDAR 采集控制台")
        self._fit_to_screen()
        self.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.log = EventLog(cfg.logs_dir)
        self.link = build_link(cfg, Path(cfg.sim_root) if cfg.is_sim else None)
        self.ctrl = Controller(cfg, self.link, self.log, emit=self.events.put)

        self.cards: dict[str, StepCard] = {}
        self._console_dirty = 0
        self._toast_until = 0.0
        self._spark_pts = 0
        self._last_size: int | None = None
        self._elapsed_base = 0.0
        self._elapsed_mark: float | None = None
        self._clock_state = "idle"

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<F5>", lambda _e: self._start())
        self.bind("<F6>", lambda _e: self._stop())
        self.bind("<F7>", lambda _e: self._reconnect())
        self.ctrl.start()
        self.after(60, self._drain)
        self.after(500, self._tick_clock)
        self._apply_steps()
        self._set_state(self.ctrl.state)
        if demo:
            self.after(800, self._demo_tick, 0)

    def _fit_to_screen(self):
        """默认给一个大窗口，但不超过屏幕的九成，免得小屏上按钮跑到屏幕外。"""
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(1640, int(screen_w * 0.94))
        height = min(1080, int(screen_h * 0.92))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(1280, width), min(940, height))

    # ------------------------------------------------------------------ 构建
    def _build(self):
        header = tk.Frame(self, bg=CARD, height=70)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

        left = tk.Frame(header, bg=CARD)
        left.pack(side="left", padx=22, pady=12)
        tk.Label(left, text="LiDAR 采集控制台", bg=CARD, fg=TEXT, font=(self.ui_font, 16, "bold")).pack(anchor="w")
        tk.Label(
            left,
            text="rk3588 · 串口透传 · tcpdump 录制 lidar.pcap",
            bg=CARD,
            fg=MUTED,
            font=(self.ui_font, 9),
        ).pack(anchor="w")

        right = tk.Frame(header, bg=CARD)
        right.pack(side="right", padx=22)
        self.clock = tk.Label(right, text="00:00", bg=CARD, fg=TEXT, font=(self.mono_font, 15, "bold"))
        self.clock.pack(side="right", padx=(14, 0))
        self.clock_caption = tk.Label(right, text="录制时长", bg=CARD, fg=MUTED, font=(self.ui_font, 8))
        self.clock_caption.pack(side="right")

        self.link_pill = tk.Frame(right, bg="#fdeaea")
        self.link_pill.pack(side="right", padx=10)
        self.link_dot = tk.Canvas(self.link_pill, width=10, height=10, bg="#fdeaea", highlightthickness=0)
        self.link_dot.pack(side="left", padx=(9, 5), pady=6)
        self.link_dot_id = self.link_dot.create_oval(1, 1, 9, 9, fill=ERR, outline="")
        self.link_label = tk.Label(
            self.link_pill, text="未连接", bg="#fdeaea", fg=ERR, font=(self.ui_font, 9, "bold")
        )
        self.link_label.pack(side="left", padx=(0, 10))

        self.mode_badge = tk.Label(
            right,
            text="模拟模式" if self.cfg.is_sim else "实机模式",
            bg="#fdf1dc" if self.cfg.is_sim else ACCENT_SOFT,
            fg=WARN if self.cfg.is_sim else ACCENT,
            font=(self.ui_font, 9, "bold"),
            padx=10,
            pady=3,
        )
        self.mode_badge.pack(side="right", padx=10)

        footer = tk.Frame(self, bg=BG, height=30)
        footer.pack(fill="x", side="bottom")
        self.status = tk.Label(footer, text="就绪", bg=BG, fg=MUTED, font=(self.ui_font, 9), anchor="w")
        self.status.pack(side="left", padx=22)
        tk.Label(
            footer,
            text=f"日志：{self.cfg.logs_dir}",
            bg=BG,
            fg=FAINT,
            font=(self.mono_font, 8),
        ).pack(side="right", padx=22)

        self._build_console()

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 0))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        self._build_flow(body)
        self._build_side(body)

    def _card(self, master, title, subtitle=""):
        outer = tk.Frame(master, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(head, text=title, bg=CARD, fg=TEXT, font=(self.ui_font, 11, "bold")).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, bg=CARD, fg=FAINT, font=(self.ui_font, 8)).pack(side="right")
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill="both", expand=True, padx=18, pady=(8, 12))
        return outer, body

    def _build_flow(self, parent):
        card, body = self._card(parent, "采集流程", "连接、静音、录制、停止")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 14))

        steps = [
            ("connect", self.cfg.connect_cmd),
            ("dmesg", self.cfg.cmd_dmesg),
            ("start", self.cfg.start_cmd()),
            ("stop", self.cfg.cmd_stop),
        ]
        for index, (name, cmd) in enumerate(steps, start=1):
            sc = StepCard(body, STEP_TITLES[name], cmd, self.ui_font, self.mono_font, index=index)
            sc.pack(fill="x", pady=(0, 10))
            self.cards[name] = sc

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(2, 12))

        buttons = tk.Frame(body, bg=CARD)
        buttons.pack(fill="x")
        self.btn_start = self._button(buttons, "开始录制", self._start, primary=True)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_stop = self._button(buttons, "停止录制", self._stop, dark=True)
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.btn_reconnect = self._button(buttons, "重新连接", self._reconnect)
        self.btn_reconnect.pack(side="left", padx=(0, 8))
        self.btn_reset = self._button(buttons, "重置流程", self._reset)
        self.btn_reset.pack(side="left")

        hint = tk.Frame(body, bg="#f5f8ff")
        hint.pack(fill="both", expand=True, pady=(14, 0))
        tk.Label(
            hint,
            text="拔线后 3588 会继续录制，界面显示「连接断开」；插回线点「重新连接」，",
            bg="#f5f8ff",
            fg="#4a5b78",
            font=(self.ui_font, 9),
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=14, pady=(12, 0))
        tk.Label(
            hint,
            text="程序会先查一次远端进程，还在录就直接接上监视，不用重新开始。",
            bg="#f5f8ff",
            fg="#4a5b78",
            font=(self.ui_font, 9),
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 12))


    def _build_side(self, parent):
        side = tk.Frame(parent, bg=BG)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)

        side.rowconfigure(0, weight=0)
        side.rowconfigure(1, weight=1)

        self.file_card, fbody = self._card(side, "pcap 文件", self.cfg.interface)
        self.file_card.pack(fill="x")

        row = tk.Frame(fbody, bg=CARD)
        row.pack(fill="x")
        tk.Label(row, text=self.cfg.remote_file, bg=CARD, fg=TEXT, font=(self.mono_font, 11, "bold")).pack(side="left")
        self.size_label = tk.Label(row, text="—", bg=CARD, fg=MUTED, font=(self.mono_font, 9))
        self.size_label.pack(side="right")

        self.big_size = tk.Label(
            fbody, text="等待连接", bg=CARD, fg=TEXT, font=(self.ui_font, 24, "bold"), anchor="w"
        )
        self.big_size.pack(fill="x", pady=(2, 0))

        line = tk.Frame(fbody, bg=CARD)
        line.pack(fill="x")
        self.verdict_pill = tk.Label(
            line, text="未开始", bg="#eef1f7", fg=MUTED, font=(self.ui_font, 9, "bold"), padx=10, pady=2
        )
        self.verdict_pill.pack(side="left")
        self.rate_label = tk.Label(line, text="", bg=CARD, fg=MUTED, font=(self.mono_font, 9))
        self.rate_label.pack(side="right")

        self.spark = Sparkline(fbody)
        self.spark.pack(fill="x", pady=(6, 2))

        self.session_card, sbody = self._card(side, "本次会话", "停止后日志与 pcap 一起覆盖")
        self.session_card.pack(fill="both", expand=True, pady=(12, 0))

        self.session_rows = {}
        for key, label in [
            ("link", "链路"),
            ("proc", "远端 tcpdump"),
            ("log", "日志目录"),
        ]:
            row = tk.Frame(sbody, bg=CARD)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=CARD, fg=MUTED, font=(self.ui_font, 9)).pack(side="left")
            value = tk.Label(
                row,
                text="—",
                bg=CARD,
                fg=TEXT,
                font=(self.mono_font, 9),
                anchor="e",
                justify="right",
                wraplength=260,
            )
            value.pack(side="right")
            self.session_rows[key] = value
        self.session_rows["log"].configure(text=str(self.cfg.logs_dir), font=(self.mono_font, 8), fg=MUTED)

        tip = tk.Frame(sbody, bg="#f5f8ff")
        tip.pack(fill="x", pady=(8, 0))
        tk.Label(
            tip,
            text="pcap 在长 → 录制中\n进程没了且大小不变 → 已停止",
            bg="#f5f8ff",
            fg="#4a5b78",
            font=(self.ui_font, 8),
            wraplength=300,
            justify="left",
            anchor="w",
            padx=10,
            pady=8,
        ).pack(fill="x")

    def _build_console(self):
        wrap = tk.Frame(self, bg=BG, height=200)
        wrap.pack(fill="x", side="bottom", padx=18, pady=(12, 0))
        wrap.pack_propagate(False)
        shell = tk.Frame(wrap, bg=CONSOLE_BG, highlightbackground=BORDER, highlightthickness=1)
        shell.pack(fill="both", expand=True)

        bar = tk.Frame(shell, bg="#182338", height=38)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        self.tabs = {}
        tabbox = tk.Frame(bar, bg="#182338")
        tabbox.pack(side="left", padx=12, pady=6)
        for key, label in [("serial", "串口输出"), ("events", "事件日志")]:
            btn = tk.Label(
                tabbox,
                text=label,
                bg="#182338",
                fg="#8fa3c4",
                font=(self.ui_font, 9, "bold"),
                padx=12,
                pady=3,
                cursor="hand2",
            )
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda _e, k=key: self._show_tab(k))
            self.tabs[key] = btn

        tk.Label(bar, text="F5 开始 · F6 停止 · F7 重新连接", bg="#182338", fg="#5b6d8c", font=(self.ui_font, 8)).pack(
            side="right", padx=14
        )
        tk.Button(
            bar,
            text="打开日志文件夹",
            command=self._open_logs,
            bg="#22304a",
            fg="#c8d5ea",
            activebackground="#2b3c5c",
            activeforeground="#ffffff",
            font=(self.ui_font, 8),
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            padx=10,
        ).pack(side="right", padx=6, pady=7)

        self.texts: dict[str, tk.Text] = {}
        holder = tk.Frame(shell, bg=CONSOLE_BG)
        holder.pack(fill="both", expand=True)
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)

        serial = tk.Text(
            holder,
            bg=CONSOLE_BG,
            fg=CONSOLE_FG,
            insertbackground=CONSOLE_BG,
            font=(self.mono_font, 9),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            wrap="none",
            height=5,
            insertwidth=0,
            takefocus=0,
        )
        serial.tag_configure("dim", foreground="#7f90ad")
        serial.tag_configure("prompt", foreground="#67d4a8")
        serial.tag_configure("ours", foreground="#7fb2ff")
        serial.grid(row=0, column=0, sticky="nsew")

        ev = tk.Text(
            holder,
            bg=CONSOLE_BG,
            fg=CONSOLE_FG,
            insertbackground=CONSOLE_BG,
            font=(self.ui_font, 9),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=12,
            pady=8,
            wrap="word",
            height=5,
            insertwidth=0,
            takefocus=0,
        )
        ev.tag_configure("time", foreground="#5b6d8c")
        ev.tag_configure("ok", foreground="#67d4a8")
        ev.tag_configure("warn", foreground="#f0b45c")
        ev.tag_configure("err", foreground="#ff8b8b")
        ev.grid(row=0, column=0, sticky="nsew")

        self.texts = {"serial": serial, "events": ev}
        self.active_tab = "serial"
        ev.lift()
        serial.lift()
        self._show_tab("serial")

    def _button(self, master, text, command, primary=False, dark=False):
        if primary:
            bg, fg, hover = ACCENT, "#ffffff", "#2457c5"
        elif dark:
            bg, fg, hover = DARK, "#ffffff", "#333f57"
        else:
            bg, fg, hover = "#e9eefa", ACCENT, "#dbe5fb"
        return tk.Button(
            master,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            font=(self.ui_font, 10, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
            padx=18,
            pady=8,
        )

    # ------------------------------------------------------------------ 动作
    def _start(self):
        self.ctrl.start_recording()

    def _stop(self):
        self.ctrl.stop_recording()

    def _reconnect(self):
        self.ctrl.reconnect()

    def _reset(self):
        self.ctrl.reset()
        for card in self.cards.values():
            card.set("pending")
        self.spark.points.clear()
        self.spark.redraw()
        self.big_size.configure(text="等待连接", fg=TEXT)
        self.rate_label.configure(text="")
        self.verdict_pill.configure(text="未开始", bg="#eef1f7", fg=MUTED)
        self._toast("流程已重置")

    def _open_logs(self):
        path = str(self.cfg.logs_dir)
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            self._toast(f"日志目录：{path}")

    def _show_tab(self, key):
        self.active_tab = key
        for name, btn in self.tabs.items():
            btn.configure(bg="#2a3b5c" if name == key else "#182338", fg="#ffffff" if name == key else "#8fa3c4")
        self.texts[key].lift()

    def _demo_tick(self, phase):
        if phase == 0:
            self.ctrl.connect()
            self.after(2200, self._demo_tick, 1)
        elif phase == 1:
            self.ctrl.start_recording()
            self.after(6000, self._demo_tick, 2)
        elif phase == 2:
            self.ctrl.stop_recording()

    # ------------------------------------------------------------------ 事件泵
    ACTIVE_CLOCK = ("starting", "recording", "stopping", "stop_failed", "disconnected")

    def _tick_clock(self):
        """拔线后 3588 还在录，界面上的计时也得继续走，不能跟着最后一次上报停住。"""
        if self._elapsed_mark is not None and self._clock_state in self.ACTIVE_CLOCK:
            secs = self._elapsed_base + (time.monotonic() - self._elapsed_mark)
            self.clock.configure(text=human_duration(secs))
        self.after(500, self._tick_clock)

    def _drain(self):
        try:
            for _ in range(200):
                ev = self.events.get_nowait()
                self._handle(ev)
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def _handle(self, ev: dict):
        kind = ev.get("type")
        if kind == "text":
            self._console_text(ev["text"], tag=ev.get("tag"))
        elif kind == "state":
            self._set_state(ev["state"])
        elif kind == "step":
            self._apply_step(ev.get("key") or ev.get("step"), ev["status"], ev.get("detail", ""))
        elif kind == "size":
            self._on_size(ev)
        elif kind == "proc":
            self._session("proc", "运行中" if ev["running"] else "未发现")
        elif kind == "link":
            self._set_link(ev["connected"], ev.get("detail", ""))
        elif kind == "elapsed":
            self._elapsed_base = float(ev["seconds"])
            self._elapsed_mark = time.monotonic()
        elif kind == "toast":
            self._toast(ev["text"], ev.get("level", "info"))
        elif kind == "manual":
            self._console_text(f"\n$ {ev.get('cmd', '')}\n{ev.get('out', '')}\n", tag="ours")
        elif kind == "stopcheck":
            self._event_line(
                ev.get("detail", ""), "warn" if ev.get("state") == "stopping" else ("ok" if ev.get("state") == "stopped" else "err")
            )
        elif kind == "log_reset":
            self.texts["serial"].delete("1.0", "end")
            self.texts["events"].delete("1.0", "end")

    def _set_state(self, state: str):
        self._clock_state = state
        if state in ("idle", "stopped"):
            self._elapsed_mark = None
        label, color = STATE_LABEL.get(state, (state, MUTED))
        self.status.configure(text=f"{label} · {self.cfg.profile} 模式")
        running = state in ("recording", "starting")
        self.btn_start.configure(state="normal" if state in ("ready", "dmesg_ok", "stopped", "stop_failed", "recording") else "disabled")
        self.btn_stop.configure(state="normal" if running else "disabled")
        self.btn_reconnect.configure(state="normal" if state in ("disconnected", "error", "recording", "stopped") else "normal")
        self._session("log", str(self.cfg.logs_dir))
        if state in ("disconnected", "error"):
            self._set_link(False)

    def _set_link(self, connected: bool, detail: str = ""):
        bg = "#e4f7ec" if connected else "#fdeaea"
        fg = OK if connected else ERR
        self.link_pill.configure(bg=bg)
        self.link_dot.configure(bg=bg)
        self.link_dot.itemconfigure(self.link_dot_id, fill=fg)
        self.link_label.configure(text="已连接 3588" if connected else "未连接 3588", bg=bg, fg=fg)
        self._session("link", "串口在线" if connected else (detail or "串口断开"))

    def _apply_steps(self):
        for name in STEP_ORDER:
            self._apply_step(name, "pending", "")

    def _apply_step(self, name, status, detail):
        card = self.cards.get(name)
        if card:
            card.set(status, detail)

    def _on_size(self, ev):
        size = ev.get("size")
        verdict = ev.get("verdict")
        rate = ev.get("rate")
        if size is None:
            self.big_size.configure(text="未开始", fg=FAINT, font=(self.ui_font, 18, "bold"))
            self.size_label.configure(text="尚未生成")
            self.spark.push(None)
        else:
            self.big_size.configure(text=human_size(size), fg=TEXT, font=(self.ui_font, 24, "bold"))
            self.size_label.configure(text=f"{size:,} 字节")
            self.spark.push(size)
            self._last_size = size
        pill = {
            "growing": ("正在录制", "#e4f7ec", OK),
            "stalled": ("大小不变", "#fdf1dc", WARN),
            "empty": ("空文件", "#fdf1dc", WARN),
            "missing": ("文件不存在", "#eef1f7", MUTED),
            "shrunk": ("文件变小", "#eef1f7", MUTED),
        }.get(verdict or "", ("采样中", "#eef1f7", MUTED))
        self.verdict_pill.configure(text=pill[0], bg=pill[1], fg=pill[2])
        self._spark_pts += 1
        self.rate_label.configure(
            text=f"≈ {human_rate(rate)} · 第 {self._spark_pts} 次采样" if rate else f"第 {self._spark_pts} 次采样"
        )

    def _session(self, key, value):
        label = self.session_rows.get(key)
        if label:
            label.configure(text=value)

    def _toast(self, text, level="info"):
        color = {"ok": OK, "warn": WARN, "err": ERR}.get(level, MUTED)
        self.status.configure(text=text, fg=color)
        self._toast_until = time.time() + 6
        tag = {"ok": "ok", "warn": "warn", "err": "err"}.get(level)
        if tag:
            self._event_line(text, tag)

    def _event_line(self, text, tag=None):
        if not text:
            return
        widget = self.texts["events"]
        widget.configure(state="normal")
        widget.insert("end", time.strftime("%H:%M:%S  "), "time")
        widget.insert("end", text + "\n", tag or ())
        self._trim(widget)
        widget.configure(state="disabled")
        widget.see("end")

    def _console_text(self, text, tag=None):
        widget = self.texts["serial"]
        widget.configure(state="normal")
        widget.insert("end", text, tag or ())
        self._trim(widget)
        widget.configure(state="disabled")
        widget.see("end")

    @staticmethod
    def _trim(widget: tk.Text):
        try:
            if float(widget.index("end-1c").split(".")[0]) > 2000:
                widget.delete("1.0", "600.0")
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self):
        try:
            self.ctrl.shutdown()
            self.ctrl.join(timeout=3)
        finally:
            try:
                self.link.close()
            except Exception:  # noqa: BLE001
                pass
            self.destroy()


def run(cfg: Config, demo: bool = False):
    app = App(cfg, demo=demo)
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(run(Config()))
