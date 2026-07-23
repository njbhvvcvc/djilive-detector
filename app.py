"""
DJI 实时视觉识别 - 统一双页面应用（开箱即用）
=================================================================
一个 tkinter 程序，两张页面：
  首页  ：开启直播 / 开启识别(开关+精度) / 播放本地文件 / 全局 LUT
  播放页：全屏视频 + 顶部显示「实际调用模型名」+「GPU 功率」
          + 顶部快捷调参 + 进度条拖动 + LUT 浓度

识别内容：电线杆 / 电线 / 绝缘子（Thalos 或 TIC）、鸟类 / 高铁（COCO）。
危险靠近红色预警；高铁/鸟顶部青色「看见提醒」。
复用：detector / alarm / sighting / capture / utils / trajectory / lut。
"""
import os
import sys
import time
import threading
import random
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import cv2
import numpy as np
from PIL import Image, ImageTk

import torch  # 设备检测

from detector import MultiModelDetector
from utils import draw_detections, FPSCounter
from alarm import analyze as alarm_analyze, draw_alarm as alarm_draw, maybe_beep as alarm_beep
from sighting import analyze as sighting_analyze, draw as sighting_draw, maybe_capture as sighting_capture
from capture import Capturer, find_window_hwnd, get_window_rect
from trajectory import TrajectoryTracker
from rtmp_server import RtmpServer, get_lan_ip
import lut as lutmod
import multiprocessing as mp
import queue
from frame_analysis import analyze_frame, apply_quality
from rtmp_source import VideoSource
from music_engine import MusicEngine, LRCParser
from music_page import MusicPage

# ---------- 诊断日志：把 stdout/stderr 同时落盘，便于无控制台 exe 排错 ----------
import datetime as _dt
try:
    _LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log")
    class _Tee:
        def __init__(self, fh, orig):
            self.fh = fh
            self.orig = orig
        def write(self, s):
            try:
                self.fh.write(s); self.fh.flush()
            except Exception:
                pass
            try:
                self.orig.write(s)
            except Exception:
                pass
        def flush(self):
            try:
                self.fh.flush()
            except Exception:
                pass
    _logf = open(_LOG_PATH, "a", encoding="utf-8")
    sys.stdout = _Tee(_logf, sys.stdout)
    sys.stderr = _Tee(_logf, sys.stderr)
    sys.stderr.write(f"\n===== app 启动 {_dt.datetime.now()} =====\n")
    sys.stderr.flush()
except Exception as _log_err:
    pass  # 日志落盘失败不应影响主程序


# =========================================================================
# 通用工具
# =========================================================================
def cfg_path_resolve():
    if getattr(sys, "frozen", False):
        try:
            base = sys._MEIPASS
            p = os.path.join(base, "config.yaml")
            if os.path.exists(p):
                return p
        except Exception:
            pass
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "config.yaml")
    return p if os.path.exists(p) else "config.yaml"


def load_config(path="config.yaml"):
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def detect_device():
    """检测并返回 (device文本, 是否Cuda, 显存GB)"""
    try:
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            name = torch.cuda.get_device_name(0)
            mem = torch.cuda.get_device_properties(0).total_memory / 1024 ** 3
            return f"GPU: {name} ({mem:.1f}GB)", True, mem
        return "CPU（无 CUDA）", False, 0
    except Exception:
        return "CPU（检测异常）", False, 0


QUALITY_INFO = {
    "high": "Thalos 1280 ~8FPS（最清晰）",
    "medium": "Thalos 960 ~12FPS（均衡）",
    "fast": "TIC 960 ~25FPS（最流畅）",
}


# =========================================================================
# 帧分析 / 绘制（与检测器协作）
# =========================================================================
# 帧分析逻辑已迁至 frame_analysis.py（便于在独立进程内导入，无 tkinter 依赖）。
# 这里仅保留与绘制相关、且本就运行在主进程的内容。


def draw_all(disp, dets, alarm_res, atrig, sighted, cfg, states, t):
    draw_detections(disp, dets, cfg.get("display", {}))
    if alarm_res is not None:
        alarm_draw(disp, alarm_res, cfg.get("alarm", {}), blink_phase=t)
    if atrig:
        from alerts import draw as alerts_draw
        alerts_draw(disp, atrig, cfg.get("alerts", {}), t=t)
    if sighted:
        sighting_draw(disp, sighted, cfg.get("sighting", {}), t=t)
    return disp


# =========================================================================
# LUT 全局控件（首页 / 播放页共用 app 状态）
# =========================================================================
def build_lut_controls(parent, app, show_slider=True):
    """构建一个 LUT 选择控件（含导入按钮 + 浓度滑块），共享 app 全局状态。"""
    frame = ttk.Frame(parent)
    ttk.Label(frame, text="调色 LUT：").grid(row=0, column=0, sticky="w")
    combo = ttk.Combobox(frame, textvariable=app.lut_selected,
                          values=list(app.lut_cache.keys()),
                          state="readonly", width=14)
    combo.grid(row=0, column=1, padx=(0, 4))
    app._lut_combos.append(combo)
    ttk.Button(frame, text="导入 .cube",
               command=lambda: _import_lut(app)).grid(row=0, column=2, padx=(0, 6))

    def _apply_sel(e=None):
        lab = app.lut_selected.get()
        lut3d, size = app.lut_cache.get(lab, (None, 0))
        app.lut3d = lut3d
        app.lut_size = size
    combo.bind("<<ComboboxSelected>>", _apply_sel)
    _apply_sel()

    if show_slider:
        ttk.Label(frame, text="浓度").grid(row=0, column=3, padx=(0, 2))
        sc = ttk.Scale(frame, from_=0, to=200, variable=app.lut_conc,
                       orient="horizontal", length=120)
        sc.grid(row=0, column=4, padx=(0, 2))
        ttk.Label(frame, textvariable=app.lut_conc).grid(row=0, column=5)
    return frame


def _import_lut(app):
    p = filedialog.askopenfilename(
        title="导入 LUT (.cube)",
        filetypes=[("Cube LUT", "*.cube"), ("全部", "*.*")])
    if not p:
        return
    try:
        lut3d, size, title = lutmod.load_cube(p)
    except Exception as e:
        messagebox.showerror("LUT 导入失败", f"{type(e).__name__}: {e}")
        return
    label = os.path.basename(p)
    app.lut_cache[label] = (lut3d, size)
    for c in app._lut_combos:
        c["values"] = list(app.lut_cache.keys())
    app.lut_selected.set(label)
    app.lut3d = lut3d
    app.lut_size = size


# =========================================================================
# 挂件模式（透明置顶覆盖层，独立进程由 --overlay 拉起）
# =========================================================================
MAGENTA = (255, 0, 255)


def run_overlay(cfg, title):
    found = find_window_hwnd(title)
    if not found:
        messagebox.showerror("挂件模式", f"未找到标题含 “{title}” 的窗口。\n"
                                          f"请先打开该软件（如 DJI 飞行软件）并显示推流画面，再启动挂件。")
        return
    hwnd, wtitle = found
    try:
        rect = get_window_rect(hwnd)
    except Exception:
        rect = None
    if not rect:
        messagebox.showerror("挂件模式", "无法获取目标窗口位置。")
        return
    l, t, r, b = rect

    cap = Capturer({"mode": "window", "window_title": title,
                    "capture_width": None, "capture_height": None})
    det = MultiModelDetector(cfg.get("models", {})) if cfg.get("models", {}).get("enabled", True) else None

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", "magenta")
    root.geometry(f"{r - l}x{b - t}+{l}+{t}")
    label = tk.Label(root, bg="magenta")
    label.pack(fill="both", expand=True)

    states = {"alarm": {}, "alerts": {}, "sighting": {}, "fps": FPSCounter()}
    running = {"go": True}

    def on_close():
        running["go"] = False

    def update():
        if not running["go"]:
            try:
                cap.release()
            except Exception:
                pass
            root.destroy()
            return
        frame = cap.get_frame()
        if frame is not None:
            if det is not None:
                dets, alarm_res, atrig, sighted, info = analyze_frame(frame, det, cfg, states)
            else:
                dets, alarm_res, atrig, sighted = [], None, [], []
            disp = np.zeros_like(frame)
            disp[:] = MAGENTA
            draw_detections(disp, dets, cfg.get("display", {}))
            if alarm_res is not None:
                for d in alarm_res.get("direct", []):
                    x1, y1, x2, y2 = d["bbox"]
                    cv2.rectangle(disp, (int(x1), int(y1)), (int(x2), int(y2)),
                                  (0, 0, 255), 3)
                    cv2.putText(disp, "危", (int(x1), max(int(y1) - 8, 14)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
                if alarm_res.get("danger"):
                    cv2.putText(disp, "危险靠近预警！", (12, 26),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
                elif alarm_res.get("warn"):
                    cv2.putText(disp, "注意：目标靠近带电体", (12, 26),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 215, 255), 2)
            if sighted:
                cv2.putText(disp, "看见提醒：" + "、".join(sighted), (12, 26),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            im = Image.fromarray(rgb)
            im.thumbnail((r - l, b - t))
            ph = ImageTk.PhotoImage(im)
            label.configure(image=ph)
            label.image = ph
            states["fps"].tick()
        root.after(30, update)

    hint = tk.Label(root, text="挂件运行中 · 按 Esc 退出", bg="magenta",
                   fg="white", font=("Microsoft YaHei", 9))
    hint.place(relx=0.5, rely=1.0, anchor="s")

    def on_key(e):
        if e.keysym == "Escape":
            on_close()
    root.bind("<Escape>", on_key)
    root.after(30, update)
    root.mainloop()


# =========================================================================
# 首页
# =========================================================================
class HomePage(tk.Frame):
    # ---- 设计令牌（深色 · 玻璃拟态 · 青色强调）----
    BG_DEEP = "#070c16"
    PANEL    = "#0a1120"
    CARD     = "#0e1830"
    BORDER   = "#1c2942"
    TEXT     = "#e8eef7"
    MUTED    = "#8aa0c0"
    ACCENT   = "#22d3ee"
    ACCENT_D = "#5ce6f5"
    OKGREEN  = "#34d399"
    DANGER   = "#f87171"

    def __init__(self, master, app):
        super().__init__(master, bg=self.BG_DEEP)
        self.app = app
        self._bg_running = False
        self._bg_after = None
        self._fade_after = None
        self._bg_idx = -1
        self._bg_size = (0, 0)
        self._bg_bases = {}
        self._bg_paths = []     # 缓存的 Picsum 图片路径
        self._build()

    def _build(self):
        # ===== 背景层（Lorem Picsum 在线图片轮播，置于最底）=====
        self._bg = tk.Label(self, bg=self.BG_DEEP, borderwidth=0,
                            highlightthickness=0)
        self._bg.place(x=0, y=0, relwidth=1, relheight=1)

        # ===== 左侧控制侧栏（非对称布局）=====
        self._panel = tk.Frame(self, bg=self.PANEL,
                                highlightbackground=self.BORDER,
                                highlightthickness=1, bd=0)
        self._panel.place(x=0, y=0, relheight=1, width=480)

        self._col = tk.Frame(self._panel, bg=self.PANEL)
        self._col.pack(fill="both", expand=True, padx=22, pady=20)

        # ---- 头部 ----
        tk.Label(self._col, text="DJI 实时视觉识别",
                 bg=self.PANEL, fg="#eaf2ff",
                 font=("Microsoft YaHei", 19, "bold")).pack(anchor="w")
        tk.Frame(self._col, bg=self.ACCENT, height=3).pack(fill="x", pady=(5, 0))
        tk.Label(self._col,
                 text="电线杆 · 电线 · 鸟类 · 高铁 — 智能视觉监测",
                 bg=self.PANEL, fg=self.MUTED,
                 font=("Microsoft YaHei", 10)).pack(anchor="w", pady=(6, 2))

        dev_text, is_cuda, _ = detect_device()
        dev_color = self.OKGREEN if is_cuda else self.DANGER
        self._dev_lbl = tk.Label(self._col, text=f"运行设备：{dev_text}",
                                    bg=self.PANEL, fg=dev_color,
                                    font=("Consolas", 9))
        self._dev_lbl.pack(anchor="w", pady=(0, 12))

        # ---- 识别设置 ----
        body = self._card("识别设置")
        self._chk(body, "开启识别", self.app.recognition_on,
                  self._on_rec).pack(anchor="w")
        row = tk.Frame(body, bg=self.CARD)
        row.pack(anchor="w", pady=(6, 0))
        tk.Label(row, text="识别精度：", bg=self.CARD, fg=self.TEXT,
                 font=("Microsoft YaHei", 10)).pack(side="left")
        qbox = ttk.Combobox(row, textvariable=self.app.quality,
                             values=["high", "medium", "fast"],
                             state="readonly", width=12)
        qbox.pack(side="left", padx=(4, 0))
        qbox.bind("<<ComboboxSelected>>", lambda e: self._update_qinfo())
        self.qinfo = tk.Label(row, text="", bg=self.CARD, fg="#7f8ea3",
                              font=("Consolas", 8))
        self.qinfo.pack(side="left", padx=(8, 0))
        self._update_qinfo()

        # ---- 开启直播 ----
        body = self._card("开启直播（RTMP）")
        self._radio(body, "连接到 RTMP 服务器（客户端）", "client").pack(anchor="w")
        self._radio(body, "本机作为 RTMP 服务器（接收无人机推流）",
                   "server").pack(anchor="w", pady=(2, 0))
        self.live_hint = tk.Label(body, text="", bg=self.CARD,
                                  fg=self.MUTED, wraplength=380,
                                  justify="left", font=("Microsoft YaHei", 9))
        self.live_hint.pack(anchor="w", pady=(4, 6))
        self.live_url_box = tk.Frame(body, bg=self.CARD)
        self.live_url_box.pack(fill="x", pady=(0, 6))
        tk.Label(self.live_url_box, text="RTMP 地址（客户端模式填写）：",
                 bg=self.CARD, fg=self.TEXT,
                 font=("Microsoft YaHei", 9)).pack(anchor="w")
        self._entry(self.live_url_box, self.app.rtmp_url).pack(
            fill="x", pady=(2, 6))
        self._btn(body, "▶  开启直播", self._start_live, accent=True).pack(
            anchor="w", pady=(2, 0))
        self._refresh_live_hint()

        # ---- 播放本地文件 ----
        body = self._card("播放本地文件")
        frow = tk.Frame(body, bg=self.CARD)
        frow.pack(fill="x")
        self._entry(frow, self.app.file_path).pack(
            side="left", fill="x", expand=True)
        self._btn(frow, "浏览…", self._pick_file).pack(
            side="left", padx=(6, 0))
        self._btn(body, "▶  播放", self._start_file).pack(
            anchor="w", pady=(6, 0))

        # ---- 全局 LUT ----
        body = self._card("全局调色 LUT")
        build_lut_controls(body, self.app, show_slider=True).pack(anchor="w")
        prev_frame = tk.Frame(body, bg=self.CARD)
        prev_frame.pack(anchor="w", pady=(8, 0))
        self.preview_label = tk.Label(prev_frame, relief="sunken",
                                    bg="#070d18")
        self.preview_label.pack(side="left")
        tk.Label(prev_frame, text="（LUT 实时预览，随浓度/选择变化）",
                 bg=self.CARD, fg="#7f8ea3",
                 font=("Microsoft YaHei", 8)).pack(side="left", padx=(6, 0))
        self.app.lut_selected.trace_add("write",
            lambda *a: self._refresh_lut_preview())
        self.app.lut_conc.trace_add("write",
            lambda *a: self._refresh_lut_preview())
        self._refresh_lut_preview()

        # ---- 底部操作 ----
        bottom = tk.Frame(self._col, bg=self.PANEL)
        bottom.pack(fill="x", pady=(10, 0))
        self._btn(bottom, "挂件模式（叠在原始 exe 上）",
                  self._start_overlay).pack(side="left")
        self._btn(bottom, "退出", self.app.destroy).pack(side="right")

        # ===== 尺寸初始化 + 启动在线背景轮播 =====
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            w, h = 920, 640
        self._bg_size = (w, h)
        self._layout_sidebar()
        self.bind("<Configure>", self._on_configure)
        self._start_bg()

    # ---------- 卡片 / 控件小工具 ----------
    def _card(self, title):
        c = tk.Frame(self._col, bg=self.CARD,
                      highlightbackground=self.BORDER,
                      highlightthickness=1, bd=0)
        c.pack(fill="x", pady=(0, 10))
        tk.Label(c, text=title, bg=self.CARD, fg="#7fd8ee",
                 font=("Microsoft YaHei", 11, "bold")).pack(
            anchor="w", padx=12, pady=(8, 2))
        body = tk.Frame(c, bg=self.CARD)
        body.pack(fill="x", padx=12, pady=(2, 10))
        return body

    def _btn(self, parent, text, cmd, accent=False):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=(self.ACCENT if accent else "#16243f"),
                      fg=("#04141a" if accent else "#cfe3ff"),
                      activebackground=(self.ACCENT_D if accent else "#1e3358"),
                      activeforeground=("#04141a" if accent else "#ffffff"),
                      relief="flat", borderwidth=0,
                      font=("Microsoft YaHei", 10,
                             "bold" if accent else "normal"),
                      padx=14, pady=6, cursor="hand2")
        return b

    def _entry(self, parent, var):
        return tk.Entry(parent, textvariable=var, bg="#070d18",
                       fg="#e8eef7", insertbackground=self.ACCENT,
                       relief="flat", highlightbackground=self.BORDER,
                       highlightthickness=1, bd=0,
                       font=("Microsoft YaHei", 10))

    def _chk(self, parent, text, var, cmd=None):
        return tk.Checkbutton(parent, text=text, variable=var, command=cmd,
                             bg=self.CARD, fg=self.TEXT,
                             selectcolor="#13203a", activebackground=self.CARD,
                             activeforeground=self.TEXT,
                             font=("Microsoft YaHei", 10), cursor="hand2")

    def _radio(self, parent, text, val):
        return tk.Radiobutton(parent, text=text,
                             variable=self.app.live_mode, value=val,
                             command=self._refresh_live_hint,
                             bg=self.CARD, fg=self.TEXT,
                             selectcolor="#13203a", activebackground=self.CARD,
                             activeforeground=self.TEXT,
                             font=("Microsoft YaHei", 10), cursor="hand2")

    # ---------- 在线背景（Lorem Picsum）----------
    def _picsum_cache_dir(self):
        if getattr(sys, "_MEIPASS", None):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        d = os.path.join(base, "assets", "picsum_cache")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def _fetch_picsum(self, w, h, seed):
        """在线拉取一张 Picsum 图，存到缓存目录，返回路径；失败返回 None。"""
        w = min(max(2, int(w)), 1600)
        h = min(max(2, int(h)), 1200)
        url = f"https://picsum.photos/seed/{seed}/{w}/{h}"
        try:
            import requests
            r = requests.get(url, timeout=12)
            if r.status_code != 200 or not r.content:
                return None
            d = self._picsum_cache_dir()
            fn = os.path.join(d, f"picsum_{seed}.img")
            with open(fn, "wb") as f:
                f.write(r.content)
            return fn
        except Exception:
            return None

    def _make_scrim(self, img, w, h):
        from PIL import ImageDraw, ImageFont
        dark = Image.new("RGB", (w, h), (7, 11, 20))
        base = Image.blend(img, dark, 0.55)
        try:
            draw = ImageDraw.Draw(base)
            try:
                fnt = ImageFont.truetype("msyh.ttc", 13)
            except Exception:
                fnt = ImageFont.load_default()
            draw.text((w - 170, h - 24), "DJI 视觉识别",
                      font=fnt, fill=(120, 150, 190))
        except Exception:
            pass
        return base

    def _get_base(self, idx):
        w, h = self._bg_size
        if w < 2 or h < 2:
            return None
        key = (idx, w, h)
        if key in self._bg_bases:
            return self._bg_bases[key]
        sw = min(500, max(360, int(w * 0.52)))
        aw, ah = max(1, w - sw), h
        try:
            im = Image.open(self._bg_paths[idx]).convert("RGB")
            ow, oh = im.size
            scale = min(aw / ow, ah / oh)
            nw, nh = max(1, int(ow * scale)), max(1, int(oh * scale))
            im = im.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGB", (w, h), (7, 11, 20))
            canvas.paste(im, (sw + (aw - nw) // 2, (h - nh) // 2))
            im = canvas
        except Exception:
            return None
        base = self._make_scrim(im, w, h)
        self._bg_bases[key] = base
        return base

    def _show_base(self, idx):
        base = self._get_base(idx)
        if base is None:
            return
        ph = ImageTk.PhotoImage(base)
        self._bg.configure(image=ph)
        self._bg.image = ph

    def _start_bg(self):
        if self._bg_running:
            return
        self._bg_running = True
        # 预载本地缓存（断网也能轮播上次的图）
        try:
            d = self._picsum_cache_dir()
            for fn in os.listdir(d):
                if fn.startswith("picsum_") and fn.endswith(".img"):
                    self._bg_paths.append(os.path.join(d, fn))
            self._bg_paths.sort()
            if self._bg_paths:
                self._bg_idx = 0
                self._show_base(0)
        except Exception:
            pass
        # 后台拉取首批图（补到 4 张），断网则留空待下次轮换重试
        threading.Thread(target=self._prefetch_loop, daemon=True).start()
        self._schedule_next(5200)

    def _prefetch_loop(self):
        w, h = (self._bg_size[0] or 1280), (self._bg_size[1] or 720)
        while self._bg_running and len(self._bg_paths) < 4:
            seed = random.randint(1, 10 ** 9)
            p = self._fetch_picsum(w, h, seed)
            if p:
                self.after(0, self._on_image_ready, p)
            else:
                break

    def _on_image_ready(self, path):
        if not self._bg_running:
            return
        if path not in self._bg_paths:
            self._bg_paths.append(path)
        if self._bg_idx < 0:
            self._bg_idx = 0
            self._show_base(0)

    def _refill_one(self):
        if not self._bg_running or len(self._bg_paths) >= 12:
            return
        w, h = (self._bg_size[0] or 1280), (self._bg_size[1] or 720)
        seed = random.randint(1, 10 ** 9)
        p = self._fetch_picsum(w, h, seed)
        if p:
            self.after(0, self._on_image_ready, p)

    def _schedule_next(self, delay):
        if not self._bg_running:
            return
        self._bg_after = self.after(delay, self._rotate)

    def _rotate(self):
        if not self._bg_running:
            self._schedule_next(5200)
            return
        # 每次轮换顺手补充一张（异步，不阻塞 UI）
        threading.Thread(target=self._refill_one, daemon=True).start()
        if len(self._bg_paths) < 2:
            self._schedule_next(5200)
            return
        nxt = (self._bg_idx + 1) % len(self._bg_paths)
        self._crossfade(self._bg_idx, nxt)
        self._bg_idx = nxt
        self._schedule_next(5200)

    def _crossfade(self, a, b, steps=12):
        w, h = self._bg_size
        if w < 2 or h < 2:
            return
        imgA = self._get_base(a)
        imgB = self._get_base(b)
        if imgA is None or imgB is None:
            return
        if self._fade_after:
            try:
                self.after_cancel(self._fade_after)
            except Exception:
                pass
            self._fade_after = None

        def step(i):
            if not self._bg_running:
                return
            t = i / steps
            blended = Image.blend(imgA, imgB, t)
            ph = ImageTk.PhotoImage(blended)
            self._bg.configure(image=ph)
            self._bg.image = ph
            if i < steps:
                self._fade_after = self.after(45, lambda: step(i + 1))
            else:
                self._fade_after = None

        step(0)

    # ---------- 窗口尺寸变化：侧栏自适应 + 背景重绘 ----------
    def _on_configure(self, e):
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 2 or h < 2:
            return
        self._layout_sidebar()
        if (w, h) == self._bg_size:
            return
        self._bg_size = (w, h)
        self._bg_bases.clear()
        if self._bg_running and self._bg_paths:
            self._show_base(self._bg_idx if self._bg_idx >= 0 else 0)

    def _layout_sidebar(self):
        w = self.winfo_width()
        sw = min(500, max(360, int(w * 0.52)))
        try:
            self._panel.place_configure(width=sw)
        except Exception:
            pass

    def _update_qinfo(self):
        self.qinfo.config(text=QUALITY_INFO.get(self.app.quality.get(), ""))

    def _refresh_lut_preview(self):
        if getattr(self, "preview_label", None) is None:
            return
        sample = getattr(self.app, "_lut_sample", None)
        if sample is None:
            h, w = 64, 112
            yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
            sample = np.zeros((h, w, 3), np.uint8)
            sample[..., 0] = (xx / w * 255).astype(np.uint8)   # B 横向渐变
            sample[..., 1] = (yy / h * 255).astype(np.uint8)   # G 纵向渐变
            sample[..., 2] = 110                                 # R 固定中调
            sample = cv2.cvtColor(sample, cv2.COLOR_BGR2RGB)
            self.app._lut_sample = sample
        out = sample
        if self.app.lut3d is not None and self.app.lut_conc.get() > 0:
            bgr = cv2.cvtColor(sample, cv2.COLOR_RGB2BGR)
            bgr = lutmod.apply_lut(bgr, self.app.lut3d, self.app.lut_size, self.app.lut_conc.get())
            out = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        im = Image.fromarray(out).resize((112, 64), Image.LANCZOS)
        ph = ImageTk.PhotoImage(im)
        self.preview_label.configure(image=ph, text="")
        self.preview_label.image = ph

    def _on_rec(self):
        pass

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[("视频", "*.mp4 *.avi *.mov *.mkv *.flv"), ("全部", "*.*")])
        if p:
            self.app.file_path.set(p)

    def _refresh_live_hint(self):
        if self.app.live_mode.get() == "server":
            self.live_url_box.pack_forget()
            try:
                ip = get_lan_ip()
            except Exception:
                ip = "127.0.0.1"
            url = f"rtmp://{ip}:1935/live/drone1"
            self.live_hint.config(
                foreground="#c0392b",  # 红字，醒目
                text=f"★ 本机作为 RTMP 服务器，接收无人机推流 ★\n"
                      f"请把下方地址完整填进无人机的「RTMP 推流」设置：\n"
                      f"{url}\n"
                      f"（注意：要填你电脑的局域网 IP，不是 127.0.0.1）")
        else:
            self.live_url_box.pack(fill="x", pady=(0, 6))
            self.live_hint.config(foreground="gray",
                                 text="以客户端身份连接到已有的 RTMP 服务器。")

    def _start_live(self):
        # 若直播已在后台运行（仅被切到别的页面隐藏），直接切回，
        # 不重复创建 RTMP 收流服务器（端口 1935 已被占用会失败）。
        if (self.app.player is not None
                and getattr(self.app.player, "_page_mode", None) == "stream"
                and not self.app.player.winfo_viewable()):
            self.app.show_video()
            return
        if self.app.live_mode.get() == "server":
            print(f"[_start_live] 进入服务器模式，准备启动 RTMP 收流服务器…")
            try:
                srv = RtmpServer(listen_port=1935, stream="live/drone1", local_port=1936)
                print(f"[_start_live] RtmpServer 已构造，ffmpeg={srv.ffmpeg_exe}")
                if not srv.start():
                    messagebox.showerror("直播服务器",
                        "RTMP 收流服务器启动失败（端口 1935 可能被占用，或 ffmpeg 参数错误）。\n"
                        f"ffmpeg 路径: {srv.ffmpeg_exe}\n"
                        "请查看控制台 [ffmpeg] 输出的真实报错。")
                    return
                print(f"[_start_live] 服务器启动成功，proc.poll={srv.proc.poll() if srv.proc else 'None'}")
            except Exception as e:
                import traceback as _tb
                print("[_start_live] 异常:", _tb.format_exc())
                messagebox.showerror("直播启动失败",
                    f"{type(e).__name__}: {e}\n\n（若提示找不到 ffmpeg，请使用最新打包的 exe。）")
                return
            # 启动自检：确认 ffmpeg 进程确实在运行（端口 1935 已 bind）
            import time as _t
            _t.sleep(0.6)
            if srv.proc is None or srv.proc.poll() is not None:
                srv.stop()
                messagebox.showerror("直播服务器",
                    "收流服务器进程已退出 / 未成功监听 1935 端口。\n"
                    "请查看控制台 [ffmpeg] 输出的真实报错。")
                return
            self.app.rtmp_server = srv
            # OpenCV 读取本机 udp 流（由 ffmpeg 推送进程提供）
            self.app.show_player("stream", srv.local_url, server=srv)
            return
        url = self.app.rtmp_url.get().strip()
        if not url:
            messagebox.showerror("直播", "请填写 RTMP 推流地址。")
            return
        self.app.show_player("stream", url)

    def _start_file(self):
        p = self.app.file_path.get().strip()
        if not p or not os.path.exists(p):
            messagebox.showerror("文件", "请先选择本地视频文件。")
            return
        # 若同文件已在后台播放，直接切回，不重复创建播放页
        if (self.app.player is not None
                and getattr(self.app.player, "_page_mode", None) == "file"
                and getattr(self.app.player, "_page_target", None) == p
                and not self.app.player.winfo_viewable()):
            self.app.show_video()
            return
        self.app.show_player("file", p)

    def _start_overlay(self):
        title = simpledialog_title()
        if not title:
            return
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "--overlay", title])
        except Exception as e:
            messagebox.showerror("挂件", f"无法启动挂件进程：{e}")


def simpledialog_title():
    import tkinter.simpledialog as sd
    return sd.askstring("挂件模式", "目标窗口标题（含）：", initialvalue="DJI")


# =========================================================================
# 播放页

# =========================================================================
# 检测器桥接：独立进程跑推理，不抢主进程 GIL（播放流畅 / 推理只作叠加层）
# =========================================================================
class DetectorBridge:
    """主进程侧检测器桥接。

    - 优先用独立子进程跑推理：推理在自有 GIL 中运行，不抢占主进程的
      读帧/渲染 GIL，因此「播放按原帧率流畅、AI 推理作为叠加层」两者兼得。
    - 若子进程启动失败（极少见），自动回退到同进程线程，功能不退化，
      仅在机器/环境异常时可能偶发卡顿。

    主线程每帧调用 feed() 投喂最新帧、poll() 取最新结果（均为非阻塞）。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.ok = False
        self._latest = ([], None, [], [], "")
        self._last_feed = 0.0
        self._running = True
        self._feed_frame = None
        self._pending = None
        self._tracking = False
        self._cascade = False
        self._conf = 0.3  # 当前识别灵敏度（置信度阈值），与 App.sensitivity 同步
        self.model_text = "加载中…"
        self.in_q = None
        self.out_q = None
        self.proc = None
        self._thread = None
        try:
            self.in_q = mp.Queue(maxsize=1)
            self.out_q = mp.Queue(maxsize=1)
            from det_worker import detector_process
            self.proc = mp.Process(target=detector_process,
                                  args=(self.in_q, self.out_q, cfg),
                                  daemon=True)
            self.proc.start()
            self.ok = True
        except Exception as e:
            print("[detector] 独立进程启动失败，回退线程模式:", repr(e))
            self.ok = False
            self._thread = threading.Thread(target=self._threaded_work,
                                          daemon=True)
            self._thread.start()

    # ---------- 主线程调用 ----------
    def build(self, models_cfg, quality, tracking, cascade):
        self._pending = apply_quality(models_cfg, quality)
        self._tracking = tracking
        self._cascade = cascade
        self._last_build = (models_cfg, quality, tracking, cascade)
        if not self.ok:
            return
        try:
            self.in_q.put_nowait(("BUILD", models_cfg, quality,
                                   tracking, cascade))
        except queue.Full:
            pass

    def set_cfg(self, tracking, cascade):
        if not self.ok:
            return
        try:
            self.in_q.put_nowait(("CFG", tracking, cascade))
        except queue.Full:
            pass

    def set_conf(self, conf):
        """实时调节识别灵敏度（置信度阈值）。conf 越低越灵敏。"""
        try:
            self._conf = float(conf)
        except Exception:
            return
        if not self.ok:
            return
        try:
            self.in_q.put_nowait(("CONF", self._conf))
        except queue.Full:
            pass

    def feed(self, frame, t):
        if frame is None:
            return
        self._feed_frame = frame  # 供回退线程使用
        if not self.ok:
            return
        if t - self._last_feed < 0.1:  # 节流 ~10fps，叠加层足够
            return
        self._last_feed = t
        try:
            # 降采样后再序列化：检测内部本就会重采样到 imgsz，故输入降到
            # 480 宽（约 0.4MB）对画质无影响，却把主进程 feeder 线程的
            # pickle 体积从 2.7MB 砍到 1/7，大幅降低其对 GIL 的占用，
            # 避免「每喂一帧就有 ~20ms 爆发」周期性卡住 UI 渲染线程 → 一顿一顿。
            h, w = frame.shape[:2]
            if w > 480:
                small = cv2.resize(frame, (480, int(h * 480.0 / w)),
                                      interpolation=cv2.INTER_AREA)
            else:
                small = frame
            self.in_q.put_nowait(("FRAME", small, t))
        except queue.Full:
            pass  # worker 正忙，丢弃本帧，下一帧再说

    def poll(self):
        if not self.ok:
            return self._latest  # 回退线程模式：直接返回内部最新结果
        # 看门狗：子进程若意外退出（CUDA 上下文丢失 / 驱动重置 / 异常），
        # 自动重建并重新加载模型，识别不中断（自愈）。
        if self.proc is None or not self.proc.is_alive():
            print("[detector] 子进程已退出，自动重启…")
            self._restart()
        try:
            while True:
                r = self.out_q.get_nowait()
                if isinstance(r, tuple) and r and isinstance(r[0], str):
                    if r[0] == "MODEL":
                        self.model_text = r[1]
                        continue
                    if r[0] == "ERR":
                        print("[detector] 进程内错误:", r[1])
                        continue
                self._latest = r
        except queue.Empty:
            pass
        return self._latest

    def _restart(self):
        """重建检测子进程（自愈）。用上次的 build 参数重新加载模型。"""
        try:
            if self.proc is not None:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.in_q = mp.Queue(maxsize=1)
            self.out_q = mp.Queue(maxsize=1)
            from det_worker import detector_process
            self.proc = mp.Process(target=detector_process,
                                  args=(self.in_q, self.out_q, self.cfg),
                                  daemon=True)
            self.proc.start()
            self.ok = True
        except Exception as e:
            print("[detector] 重启失败，回退线程模式:", repr(e))
            self.ok = False
            self._thread = threading.Thread(target=self._threaded_work,
                                          daemon=True)
            self._thread.start()
            return
        # 用上次参数重新触发模型构建
        if getattr(self, "_last_build", None) is not None:
            self.build(*self._last_build)

    def stop(self):
        self._running = False
        if self.ok and self.proc is not None:
            try:
                self.in_q.put_nowait(None)
            except Exception:
                pass
            self.proc.join(timeout=2)
            if self.proc.is_alive():
                self.proc.terminate()
        elif self._thread is not None:
            self._thread.join(timeout=2)

    # ---------- 回退：同进程线程（复刻原 _detection_worker 逻辑）----------
    def _threaded_work(self):
        from frame_analysis import analyze_frame
        from detector import MultiModelDetector
        from trajectory import TrajectoryTracker
        from utils import FPSCounter
        det = None
        tracker = TrajectoryTracker(predict_ttl=0.8)
        states = {"fps": FPSCounter(), "alarm": {}, "alerts": {}, "sighting": {}}
        while self._running:
            f = self._feed_frame
            if f is None:
                time.sleep(0.005)
                continue
            if det is None:
                if self._pending is None:
                    time.sleep(0.05)
                    continue
                try:
                    det = MultiModelDetector(self._pending)
                    det.tracking = self._tracking
                    det.auto_cascade = self._cascade
                    det.conf_override = self._conf
                    self.model_text = " · ".join(det.active_models()) + \
                        f" [{'CUDA' if det.device == 'cuda' else 'CPU'}]"
                except Exception as e:
                    self.model_text = f"模型加载失败: {e}"
                    det = None
                self._pending = None
                continue
            if det is not None:
                det.conf_override = self._conf
            try:
                states["fps"].tick()  # 回退线程每帧也累加，驱动 FPS 显示
                d, ar, at, si, info = analyze_frame(f, det, self.cfg, states)
                tracker.update(d, time.time())
                interp = tracker.interpolate(time.time())
                all_d = d + [di for di in interp if di.get("_predicted")]
                self._latest = (all_d, ar, at, si, info)
            except Exception as e:
                print("[detector] 线程推理异常:", e)
                time.sleep(0.05)


# =========================================================================
class PlayerPage(tk.Frame):
    def __init__(self, master, app, mode, target, server=None):
        super().__init__(master)
        self.server = server  # RTMP 收流服务器（服务器模式时非 None）
        self.app = app
        self.mode = mode              # "stream" | "file"
        self.target = target         # RTMP url 或 文件路径
        self.running = True
        self.paused = False
        self._seeking = False
        self._programmatic = False
        self._fullscreen = False
        self._last_shown = None

        # 文件模式：读帧线程按原帧率控速，UI 只渲染最新帧（与渲染解耦，杜绝慢动作）
        self._latest_raw = None
        self._frame_seq = 0
        self._last_seq = -1
        self._frame_pos = 0
        self._eof = False

        # 检测线程共享
        self.det = None
        self.det_lock = threading.Lock()
        self.latest_dets = []
        self.latest_alarm = None
        self.latest_atrig = []
        self.latest_sighted = []
        self.latest_info = ""
        self._frame_for_det = [None]
        self._det_build_pending = None
        self._det_size = (480, 270)   # 检测输入帧尺寸（feed 降采样后），用于坐标映射
        self._last_log_t = 0.0
        self.states = {"fps": FPSCounter(), "alarm": {}, "alerts": {}, "sighting": {}}
        self.tracker = TrajectoryTracker(predict_ttl=0.8)

        # 顶部文本（仅主线程写 widget）
        self.model_text = "加载中…"
        self.gpu_text = "GPU: …"

        self._build_ui()

        # 初始化帧源
        self._init_source()

        # 检测器桥接：优先独立进程跑推理（不抢主进程 GIL，播放绝不卡顿）；
        # 若子进程启动失败，自动回退到同进程线程（功能不退化）。
        self.det_bridge = DetectorBridge(self.app.cfg)
        if self.app.recognition_on.get():
            self.det_bridge.build(self.app.cfg.get("models", {}),
                                 self.app.quality.get(),
                                 self.app.tracking_on.get(),
                                 self.app.cascade_on.get())

        # GPU 监控线程
        self.gpu_stop = threading.Event()
        threading.Thread(target=self._gpu_loop, daemon=True).start()

        # 读帧线程：文件模式按 src_fps 精确控速；直播/服务器模式实时读管道。
        # 【关键】直播服务器模式必须也启动此线程，否则没人从 ffmpeg stdout 管道
        # 读帧，管道缓冲区（64KB）瞬间被一帧（约 2.7MB）塞满 → ffmpeg 阻塞在写
        # → 无人机推不动 → DJI 报「直播异常」且本端永远黑屏。
        threading.Thread(target=self._reader_loop, daemon=True).start()

        # 识别开关 / 精度 / 追踪 / 级联 实时联动
        self.app.recognition_on.trace_add("write", self._on_recognition)
        self.app.tracking_on.trace_add("write", self._on_tracking)
        self.app.cascade_on.trace_add("write", self._on_cascade)

        self._schedule_tick()
        self._bind_keys()

        # 收流看门狗：直播模式 8 秒内没收到帧 → 提示检查无人机推流地址
        if self.mode == "stream":
            self.after(8000, self._check_no_stream)

    # ---------------- UI ----------------
    def _build_ui(self):
        # 顶部信息栏：实际模型名 + GPU 功率
        top = ttk.Frame(self)
        top.pack(side="top", fill="x")
        self.model_var = tk.StringVar(value=self.model_text)
        self.gpu_var = tk.StringVar(value=self.gpu_text)
        ttk.Label(top, textvariable=self.model_var, font=("Consolas", 10, "bold"),
                  foreground="#1565c0").pack(side="left", padx=8, pady=4)
        ttk.Label(top, textvariable=self.gpu_var, font=("Consolas", 10),
                  foreground="#6a1b9a").pack(side="right", padx=8, pady=4)

        # 收流看门狗提示条（默认隐藏，未收到推流时才显示）
        self.warn_var = tk.StringVar(value="")
        warn = ttk.Frame(self)
        warn.pack(side="top", fill="x")
        ttk.Label(warn, textvariable=self.warn_var,
                  foreground="#e67e22", font=("Consolas", 9, "bold")
                  ).pack(side="left", padx=8, pady=2)

        # 顶部快捷调参栏
        qp = ttk.Frame(self)
        qp.pack(side="top", fill="x")
        rec = ttk.Checkbutton(qp, text="识别", variable=self.app.recognition_on)
        rec.pack(side="left", padx=6)
        ttk.Label(qp, text="精度").pack(side="left")
        qb = ttk.Combobox(qp, textvariable=self.app.quality,
                            values=["high", "medium", "fast"], state="readonly", width=10)
        qb.pack(side="left", padx=(2, 6))
        qb.bind("<<ComboboxSelected>>", lambda e: self._on_quality())
        trk = ttk.Checkbutton(qp, text="追踪", variable=self.app.tracking_on)
        trk.pack(side="left", padx=6)
        cas = ttk.Checkbutton(qp, text="自动挡", variable=self.app.cascade_on)
        cas.pack(side="left", padx=6)
        # 灵敏度（置信度阈值）实时调节：左=灵敏(低阈值) 右=严格(高阈值)
        ttk.Label(qp, text="灵敏度").pack(side="left", padx=(6, 2))
        sc = ttk.Scale(qp, from_=0.01, to=0.99, variable=self.app.sensitivity,
                        length=110,
                        command=lambda v: self.det_bridge.set_conf(self.app.sensitivity.get()))
        sc.pack(side="left")
        ttk.Label(qp, textvariable=self.app.sensitivity, width=4,
                  font=("Consolas", 9)).pack(side="left", padx=(0, 6))
        ovl = ttk.Button(qp, text="挂件", command=self._launch_overlay)
        ovl.pack(side="left", padx=6)
        lut = build_lut_controls(qp, self.app, show_slider=True)
        lut.pack(side="left", padx=6)
        # 记录检测相关控件的引用，供「识别开关」联动启用/禁用
        self._w_quality, self._w_tracking, self._w_cascade = qb, trk, cas
        self._w_sens, self._w_overlay, self._w_lut = sc, ovl, lut
        # 初始按当前「识别」状态同步控件可用性
        self._set_det_controls_enabled(self.app.recognition_on.get())

        # 视频区
        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(side="top", fill="both", expand=True)
        self.video_label = tk.Label(self.video_frame, bg="black")
        self.video_label.pack(fill="both", expand=True)

        # 底部控制栏
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="◀ 返回首页", command=self._back).pack(side="left", padx=4, pady=4)
        self.play_btn = ttk.Button(bottom, text="⏸ 暂停", command=self._toggle_pause)
        self.play_btn.pack(side="left", padx=4, pady=4)
        ttk.Button(bottom, text="⛶ 全屏", command=self._toggle_fullscreen).pack(side="left", padx=4, pady=4)

        # 音乐控制组（应用内本地播放，与识别/播放互不干扰）
        mf = ttk.Frame(bottom)
        mf.pack(side="left", padx=(10, 0))
        ttk.Button(mf, text="🎵", width=3, command=self._music_toggle_lyrics).pack(side="left")
        self.music_pp = ttk.Button(mf, text="▶", width=3, command=self._music_toggle)
        self.music_pp.pack(side="left")
        self.music_song = tk.StringVar(value="♪ 音乐")
        ttk.Label(mf, textvariable=self.music_song, width=16, anchor="w").pack(side="left")
        self.music_vol = tk.DoubleVar(value=0.9)
        ttk.Scale(mf, from_=0, to=1, variable=self.music_vol, length=90,
                     command=lambda v: self.app.music_engine.set_volume(float(v))).pack(side="left")

        self.time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(bottom, textvariable=self.time_var, font=("Consolas", 9)).pack(side="right", padx=6)

        self.progress = ttk.Scale(bottom, from_=0, to=1, orient="horizontal")
        self.progress.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        self.progress.bind("<ButtonPress-1>", lambda e: self._seek_start())
        self.progress.bind("<ButtonRelease-1>", lambda e: self._seek_end())
        self.progress.configure(command=self._on_seek)

        # 音乐叠加层（歌词覆盖在视频上 + 引擎监听）
        self._build_music_ui()

    def _build_music_ui(self):
        # 歌词叠加层：覆盖在视频上，默认隐藏，🎵 按钮切换
        self.lyric_overlay = tk.Listbox(
            self.video_frame, font=("Microsoft YaHei", 13),
            bg="#0a0c16", fg="#9aa3bd",
            selectbackground="#7c5cff", selectforeground="#ffffff",
            exportselection=False, activestyle="none", relief="flat")
        self.lyric_overlay.place(relx=0, rely=0.6, relwidth=1, relheight=0.38)
        self.lyric_overlay.lower()  # 置于视频标签之下（隐藏）
        self._lyric_visible = False
        self._v_lyrics = []
        self._v_lyric_active = -1
        self.app.music_engine.add_listener(self)

    def _music_toggle_lyrics(self):
        self._lyric_visible = not self._lyric_visible
        if self._lyric_visible:
            self.lyric_overlay.lift()
            self._render_video_lyrics()
        else:
            self.lyric_overlay.lower()

    def _music_toggle(self):
        self.app.music_engine.toggle()

    # ---- 音乐引擎监听（主线程回调）----
    def on_music_track(self, name, artist):
        self.music_song.set(f"{name}" + (f"  —  {artist}" if artist else ""))
        # 视频页也加载该曲歌词
        lp = (self.app.music_engine.current or {}).get("lyric_path")
        self._load_video_lyrics(lp)

    def on_music_state(self, state):
        self.music_pp.config(text="▶" if state != "playing" else "⏸")

    def on_music_tick(self, pos, ln):
        self._update_video_lyric(pos)

    def on_music_end(self):
        pass  # 自动切歌由音乐页负责；视频页仅停在当前曲

    def on_music_lyric(self, lines):
        # 接收来自音乐页/引擎 广播的双语歌词（API 模式），刷新视频页歌词层
        if not lines:
            return
        self._v_lyrics = list(lines)
        self._v_lyric_active = -1
        self.lyric_overlay.delete(0, tk.END)
        for ln in self._v_lyrics:
            txt = ln["orig"] + (f"\n  {ln['trans']}" if ln.get("trans") else "")
            self.lyric_overlay.insert(tk.END, txt)
        if self._lyric_visible:
            self._render_video_lyrics()

    def _load_video_lyrics(self, lyric_path):
        self._v_lyrics = []
        self._v_lyric_active = -1
        self.lyric_overlay.delete(0, tk.END)
        if lyric_path and os.path.isfile(lyric_path):
            try:
                with open(lyric_path, "r", encoding="utf-8", errors="ignore") as f:
                    orig_text = f.read()
                trans_text = ""
                tr = os.path.splitext(lyric_path)[0] + ".tr.lrc"
                if os.path.isfile(tr):
                    with open(tr, "r", encoding="utf-8", errors="ignore") as f:
                        trans_text = f.read()
                self._v_lyrics = LRCParser.load_pair(orig_text, trans_text)
            except Exception as e:
                print("[music] 视频页歌词解析失败:", e)
        if not self._v_lyrics:
            self.lyric_overlay.insert(tk.END, "（暂无歌词 / 未找到 .lrc）")
            return
        for ln in self._v_lyrics:
            txt = ln["orig"] + (f"\n  {ln['trans']}" if ln.get("trans") else "")
            self.lyric_overlay.insert(tk.END, txt)

    def _render_video_lyrics(self):
        if not self._lyric_visible:
            return
        if not self._v_lyrics:
            return
        pos = self.app.music_engine.position() / 1000.0
        self._update_video_lyric(self.app.music_engine.position())

    def _update_video_lyric(self, pos_ms):
        if not self._v_lyrics or not self._lyric_visible:
            return
        pos = pos_ms / 1000.0
        i = -1
        for k, ln in enumerate(self._v_lyrics):
            if ln["time"] <= pos:
                i = k
            else:
                break
        if i == self._v_lyric_active:
            return
        self._v_lyric_active = i
        self.lyric_overlay.selection_clear(0, tk.END)
        if i >= 0:
            self.lyric_overlay.selection_set(i)
            self.lyric_overlay.see(i)

    def _bind_keys(self):
        self.app.bind("<Escape>", self._on_escape)
        self.app.bind("<space>", lambda e: self._toggle_pause())

    def _check_no_stream(self):
        """收流看门狗：直播模式长时间无帧 → 提示检查推流地址。"""
        if not self.running or self.mode != "stream" or self.paused:
            return
        if self._latest_raw is None:
            try:
                ip = get_lan_ip()
            except Exception:
                ip = "127.0.0.1"
            self.warn_var.set(
                f"⚠ 未收到推流：请确认无人机推流地址为 "
                f"rtmp://{ip}:1935/live/drone1")
            self.after(4000, self._check_no_stream)  # 继续轮询直至收到帧
        else:
            self.warn_var.set("")

    def _launch_overlay(self):
        title = simpledialog_title()
        if not title:
            return
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "--overlay", title])
        except Exception as e:
            messagebox.showerror("挂件", f"无法启动挂件进程：{e}")

    # ---------------- 源 ----------------
    def _init_source(self):
        # 服务器模式：ffmpeg 已把 H.264 解码为原始 BGR 像素写入 stdout 管道，
        # 我们直接按固定尺寸读取（见 _reader_loop），绕开 OpenCV 解封装 / UDP / 关键帧全部坑。
        if self.mode == "stream" and self.server is not None:
            self._use_pipe = True
            self.src_fps = 30.0
            self.total_frames = 0
            self.frame_delay = 1
            try:
                self.progress.configure(state="disabled")  # 直播无可拖动进度
            except Exception:
                pass
            return
        # 客户端模式（连外部 RTMP）或本地文件：仍用 VideoSource
        self._use_pipe = False
        if self.mode == "stream":
            self.source = VideoSource(self.target, kind="stream", realtime=True)
        else:
            self.source = VideoSource(self.target, kind="file", realtime=False)
        try:
            self.src_fps = self.source.cap.get(cv2.CAP_PROP_FPS) or 30.0
        except Exception:
            self.src_fps = 30.0
        try:
            self.total_frames = int(self.source.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        except Exception:
            self.total_frames = 0
        self.frame_delay = max(1, int(1000 / self.src_fps)) if self.mode == "file" else 1
        if self.mode == "file" and self.total_frames > 0:
            self.progress.configure(to=max(self.total_frames - 1, 1))

    # ---------------- 检测线程（已迁至独立进程，见 DetectorBridge）----------------

    # ---------------- GPU 监控线程 ----------------
    def _gpu_loop(self):
        while not self.gpu_stop.is_set():
            try:
                r = subprocess.run(
                    ["nvidia-smi",
                     "--query-gpu=power.draw,utilization.gpu,memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=3)
                parts = [p.strip() for p in r.stdout.strip().split(",")]
                if len(parts) >= 4:
                    self.gpu_text = f"GPU {parts[0]}W · {parts[1]}% · 显存 {parts[2]}/{parts[3]}MB"
                else:
                    self.gpu_text = "GPU: N/A"
            except Exception:
                self.gpu_text = "GPU: N/A"
            time.sleep(1)

    # ---------------- 实时联动 ----------------
    def _on_recognition(self, *a):
        if self.app.recognition_on.get():
            # 子进程若曾被「纯播放」关掉 → 先自愈重启并重建模型
            if self.det_bridge.proc is None or not self.det_bridge.proc.is_alive():
                self.det_bridge._restart()
            else:
                self.det_bridge.build(self.app.cfg.get("models", {}),
                                     self.app.quality.get(),
                                     self.app.tracking_on.get(),
                                     self.app.cascade_on.get())
            # 识别开启 → 恢复相邻检测控件可用
            self._set_det_controls_enabled(True)
        else:
            # 关闭识别 → 纯 RTMP 播放器：停掉识别进程释放显卡、
            # 相邻两个检测开关（追踪/自动挡）一并关闭并禁用，画面只按元素播放
            self.det_bridge.stop()
            self.app.tracking_on.set(False)
            self.app.cascade_on.set(False)
            self._set_det_controls_enabled(False)

    def _on_quality(self):
        self.det_bridge.build(self.app.cfg.get("models", {}),
                             self.app.quality.get(),
                             self.app.tracking_on.get(),
                             self.app.cascade_on.get())

    def _on_tracking(self, *a):
        self.det_bridge.set_cfg(self.app.tracking_on.get(),
                               self.app.cascade_on.get())

    def _on_cascade(self, *a):
        self.det_bridge.set_cfg(self.app.tracking_on.get(),
                               self.app.cascade_on.get())

    def _set_det_controls_enabled(self, on):
        """识别开关联动：关闭时禁用所有检测相关控件（精度/追踪/自动挡/灵敏度/挂件/LUT），
        仅保留「识别」主开关本身可用，便于随时重新开启。"""
        names = ("_w_quality", "_w_tracking", "_w_cascade",
                 "_w_sens", "_w_overlay", "_w_lut")
        for n in names:
            w = getattr(self, n, None)
            if w is None:
                continue
            try:
                if w is getattr(self, "_w_quality", None):
                    w.config(state="readonly" if on else "disabled")
                else:
                    w.state(['!disabled'] if on else ['disabled'])
            except Exception:
                pass

    # ---------------- 播放控制 ----------------
    def _toggle_pause(self):
        if not self.winfo_viewable():
            return
        if self._eof:
            # 已播放到结尾 → 从头重播
            try:
                self.source.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            except Exception:
                pass
            self._eof = False
            self._frame_seq += 1  # 触发 UI 重渲染当前帧
        self.paused = not self.paused
        self.play_btn.config(text="▶ 播放" if self.paused else "⏸ 暂停")

    def _seek_start(self):
        self._seeking = True

    def _seek_end(self):
        self._seeking = False

    def _on_seek(self, val):
        if self._programmatic:
            return
        if self.mode != "file" or self._seeking is False:
            return
        try:
            self.source.cap.set(cv2.CAP_PROP_POS_FRAMES, int(float(val)))
        except Exception:
            pass
        self._eof = False
        self._frame_seq += 1  # 触发 UI 重渲染当前帧
        with self.det_lock:
            self.latest_dets = []
            self.latest_alarm = None
            self.latest_atrig = []
            self.latest_sighted = []
        self.tracker = TrajectoryTracker(predict_ttl=0.8)

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.app.attributes("-fullscreen", self._fullscreen)

    def _on_escape(self, e=None):
        # 页面被隐藏（在别的页）时忽略，避免误掐后台直播
        if not self.winfo_viewable():
            return
        if self._fullscreen:
            self._fullscreen = False
            self.app.attributes("-fullscreen", False)
        else:
            self._back()

    def _back(self):
        self._back_cleanup()
        self.app.show_home()

    def _back_cleanup(self):
        self.running = False
        self.gpu_stop.set()
        if getattr(self, "det_bridge", None) is not None:
            self.det_bridge.stop()
        if getattr(self, "_use_pipe", False):
            pass  # 管道模式无 VideoSource 可释放
        else:
            try:
                self.source.release()
            except Exception:
                pass
        # 服务器模式：停掉 ffmpeg 收流进程
        if self.app.rtmp_server is not None:
            try:
                self.app.rtmp_server.stop()
            except Exception:
                pass
            self.app.rtmp_server = None
        try:
            self.app.unbind("<Escape>")
        except Exception:
            pass
        try:
            self.app.unbind("<space>")
        except Exception:
            pass
        try:
            self.app.music_engine.remove_listener(self)
        except Exception:
            pass
        self.app.player = None   # 显式停止后清空引用，供后续 show_player 复用判断

    # ---------------- 读帧线程（仅文件模式） ----------------
    def _reader_loop(self):
        """后台读帧线程：与 UI 渲染彻底解耦，UI 只取最新帧，绝不直连 cap。
        - 文件模式：按原视频帧率墙钟控速（渲染再慢也不拖慢播放进度）。
        - 直播模式：不控速，尽力实时读最新帧并跳掉缓冲里堆积的旧帧（低延迟）。"""
        # ---- 文件模式：按原视频帧率精确读取 ----
        if self.mode == "file":
            interval = 1.0 / max(self.src_fps, 1.0)
            next_t = time.time() + interval
            while self.running:
                if self.paused or self._seeking:
                    time.sleep(0.02)
                    next_t = time.time() + interval
                    continue
                # EOF 判定（避免结尾触发 get_frame 的 reconnect 重播）
                if self.total_frames > 0:
                    try:
                        pos = int(self.source.cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                    except Exception:
                        pos = 0
                    if pos >= self.total_frames - 1:
                        self._eof = True
                        self.paused = True
                        time.sleep(0.05)
                        continue
                f = self.source.get_frame()
                if f is None:
                    self._eof = True
                    self.paused = True
                    time.sleep(0.05)
                    continue
                with self.det_lock:
                    self._latest_raw = f
                    self._frame_for_det[0] = f
                    try:
                        self._frame_pos = int(self.source.cap.get(cv2.CAP_PROP_POS_FRAMES) or 0)
                    except Exception:
                        pass
                self._frame_seq += 1
                # 墙钟控速：累加目标时刻；落后就重新对齐（绝不加速追赶）
                next_t += interval
                sleep_t = next_t - time.time()
                if sleep_t > 0:
                    time.sleep(sleep_t)
                else:
                    next_t = time.time() + interval
            return
        # ---- 直播模式：不控速，由推流端决定帧率，实时跳帧取最新 ----
        while self.running:
            if self.paused or self._seeking:
                time.sleep(0.02)
                continue
            if self._use_pipe and self.server is not None:
                # 服务器模式：直接从 ffmpeg 管道读解码好的原始帧
                f = self.server.read_frame()
                if f is None:
                    # 进程已退出（无人机断开）→ 重启服务器重新等待推流
                    # 守卫：仅在页面仍运行时重启，避免退出播放页后僵尸复活
                    if self.running and (self.server.proc is None or self.server.proc.poll() is not None):
                        try:
                            self.server.restart()
                        except Exception:
                            pass
                    time.sleep(0.1)
                    continue
                # 正常收到一帧：计数并打印，便于区分「无人机没连上」与「连上但不显示」
                self._pipe_frames = getattr(self, "_pipe_frames", 0) + 1
                if self._pipe_frames % 30 == 0:
                    print(f"[reader] 服务器模式已收到 {self._pipe_frames} 帧，尺寸={f.shape}")

            else:
                f = self.source.get_frame()
                if f is None:
                    time.sleep(0.01)  # 暂无可读帧 / 断流重连中：稍候再试，UI 显示上一帧
                    continue
            with self.det_lock:
                self._latest_raw = f
                self._frame_for_det[0] = f
            self._frame_seq += 1
            # 直播：以最快速度排空管道，避免高帧率推流在管道里堆积成延迟。
            # ffmpeg 已用 -vf fps=30 把输出封顶 30fps，且 read_frame() 是阻塞读
            # （ffmpeg 不写就读不到），读取天然被推流端节奏约束，这里不再做
            # 额外控速——只留极小让出，防止空转占满 CPU。显示端仍由
            # _schedule_tick 硬封顶 30fps，故「画面帧率」与「GPU 计算量」都受控。
            time.sleep(0.001)

    # ---------------- 显示循环 ----------------
    def _schedule_tick(self):
        if not self.running:
            return
        # 显示循环（文件/直播统一）：硬上限 30fps（~33ms），与推流/读帧节奏一致。
        # 渲染只做「取最新帧 + 叠加检测层 + 缩放上屏」，绝不跑 AI 推理
        # （推理在独立进程异步进行，慢只让「框更新」慢，绝不拖累画面流畅度）。
        self.after(33, self._tick)

    def _tick(self):
        if not self.running:
            return
        # 页面被切到其它页（仅隐藏、直播仍在后台）时，跳过渲染与识别，
        # 但 reader_loop 仍在后台排空 RTMP 管道，推流不会被无人机掐断。
        if not self.winfo_viewable():
            self._schedule_tick()
            return
        self._tick_start = time.time()
        # 顶部信息
        self.model_var.set(self.det_bridge.model_text if self.app.recognition_on.get() else "识别已关闭")
        self.gpu_var.set(self.gpu_text)
        # 播放到结尾：同步按钮（读帧线程已置 _eof/paused）
        if getattr(self, "_eof", False) and self.play_btn.cget("text") != "▶ 播放":
            self.play_btn.config(text="▶ 播放")

        frame = None
        if not self.paused:
            # 渲染线程只取读帧线程写入的最新帧（直播/文件统一）：
            # UI 线程永不直连 cap；直播下读帧线程在后台实时跳帧取最新，
            # 既消除双线程争抢同一 VideoCapture 的卡顿，也保证最低延迟。
            if getattr(self, "_frame_seq", 0) != getattr(self, "_last_seq", -1):
                frame = self._latest_raw
                self._last_seq = self._frame_seq
            if frame is not None:
                self._frame_for_det[0] = frame
        if frame is None:
            frame = self._last_shown
        if frame is None:
            h, w = 720, 1280
            frame = np.full((h, w, 3), 50, dtype=np.uint8)
            cv2.putText(frame, "等待视频流…", (30, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)

        self._last_shown = frame
        disp = frame.copy()

        # 识别叠加层（第二层级 / 最上层）：从独立进程取最新检测结果。
        # 推理在另一个进程里异步进行，慢只让「框更新」慢，绝不拖慢播放本身。
        # 检测框用归一化坐标(nbox)映射到当前显示尺寸，与原视频分层、坐标正确。
        if self.app.recognition_on.get():
            fw, fh = frame.shape[1], frame.shape[0]
            self._det_size = (480, int(fh * 480.0 / fw)) if fw > 480 else (fw, fh)
            self.det_bridge.feed(frame, time.time())
            dets, alarm_res, atrig, sighted, info = self.det_bridge.poll()
            if self.det_bridge.model_text == "加载中…":
                h, w = disp.shape[:2]
                cv2.putText(disp, "模型加载中…", (w // 2 - 90, h // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2)
            else:
                self._apply_detection_layer(disp, dets, alarm_res, atrig, sighted, info)

        # LUT 调色
        if self.app.lut3d is not None and self.app.lut_conc.get() > 0:
            disp = lutmod.apply_lut(disp, self.app.lut3d, self.app.lut_size, self.app.lut_conc.get())

        self._render(disp)

        # 进度 / 时间（拖动中不覆盖）
        if self.mode == "file" and not self._seeking:
            pos = getattr(self, "_frame_pos", 0)
            self._programmatic = True
            try:
                self.progress.set(pos)
            finally:
                self._programmatic = False
            self.time_var.set(f"{self._fmt(pos)} / {self._fmt(self.total_frames)}")

        self._schedule_tick()

    # ---------------- 检测叠加层（第二层级 / 最上层）----------------
    @staticmethod
    def _px_to_norm(bbox, det_size):
        if not bbox or not det_size:
            return None
        W, H = det_size[0], det_size[1]
        x1, y1, x2, y2 = bbox
        return (x1 / W, y1 / H, x2 / W, y2 / H)

    def _apply_detection_layer(self, disp, dets, alarm_res, atrig, sighted, info):
        """把检测结果作为「第二层级 / 最上层」叠加到画面。

        - 检测框用归一化坐标(nbox∈[0,1])映射到当前显示尺寸，
          与原视频帧率/分辨率完全解耦，坐标正确（修复旧版按 1280×720
          画 480 宽检测框导致框缩在左上角的 bug）。
        - 推理在独立进程异步运行，本函数只在 UI 线程「取最新结果」，
          慢只让框更新慢，绝不拖累播放流畅度。
        - 同时把每个框的位置坐标记录到 detections.log（满足「记录坐标」需求），
          并以「●LOCK #id」标记已锁定的稳定目标（满足「锁定」需求）。
        """
        h, w = disp.shape[:2]
        now = time.time()
        # 报警边框色（危险红 / 注意橙）
        border = None
        if alarm_res is not None:
            if alarm_res.get("danger"):
                border = (0, 0, 220)
            elif alarm_res.get("warn"):
                border = (0, 165, 255)
        # 记录坐标（最多每 0.5s 落盘一次，避免刷屏）
        if dets and (now - getattr(self, "_last_log_t", 0.0)) >= 0.5:
            self._last_log_t = now
            try:
                with open("detections.log", "a", encoding="utf-8") as _lf:
                    for d in dets:
                        nb = d.get("nbox") or self._px_to_norm(d.get("bbox"), self._det_size)
                        if not nb:
                            continue
                        _ts = time.strftime("%H:%M:%S", time.localtime(now))
                        _lbl = d.get("label", "?")
                        _cf = d.get("conf", 0.0)
                        _tid = d.get("track_id")
                        _tag = f" #{_tid}" if _tid is not None else ""
                        _lf.write(f"{_ts} {_lbl}{_tag} conf={_cf:.2f} "
                                  f"x={nb[0]:.3f} y={nb[1]:.3f} "
                                  f"w={nb[2]-nb[0]:.3f} h={nb[3]-nb[1]:.3f}\n")
            except Exception:
                pass
        # 画框（第二层级）
        for d in dets:
            nb = d.get("nbox") or self._px_to_norm(d.get("bbox"), self._det_size)
            if not nb:
                continue
            x1, y1, x2, y2 = (int(nb[0]*w), int(nb[1]*h),
                                 int(nb[2]*w), int(nb[3]*h))
            color = d.get("color", (0, 255, 0))
            cv2.rectangle(disp, (x1, y1), (x2, y2), color, 2)
            label = d.get("label", "?")
            conf = d.get("conf", 0.0)
            tid = d.get("track_id")
            txt = f"{label} {conf:.2f}"
            if tid is not None:
                txt = f"●LOCK #{tid} " + txt   # 已锁定稳定目标
            cv2.rectangle(disp, (x1, y1 - 18), (x1 + len(txt)*9 + 8, y1), color, -1)
            cv2.putText(disp, txt, (x1 + 4, y1 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            # 叠加归一化坐标（满足「记录/显示坐标」）
            cv2.putText(disp, f"({nb[0]:.2f},{nb[1]:.2f})",
                        (x1, y2 + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (200, 255, 200), 1, cv2.LINE_AA)
        if atrig:
            cv2.putText(disp, f"自定义报警 x{len(atrig)}", (10, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)
        if sighted:
            cv2.putText(disp, f"看见提醒 x{len(sighted)}", (10, 44),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 255), 1, cv2.LINE_AA)
        if border is not None:
            cv2.rectangle(disp, (2, 2), (w - 3, h - 3), border, 4)
        cv2.putText(disp, info, (10, disp.shape[0] - 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA)

    def _render(self, frame):
        h, w = frame.shape[:2]
        lw = max(1, self.video_label.winfo_width())
        lh = max(1, self.video_label.winfo_height())
        scale = min(lw / w, lh / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # 缩放上屏用 cv2.resize（720p 实测约 4ms，PIL LANCZOS 需 37ms、慢 9 倍），
        # 这是「一下一下卡」的真正根因——渲染太慢跟不上帧率。这里只做显示缩放，
        # 不改播放节奏、不做推理，纯粹把当前帧尽快画上屏。
        if (nw, nh) != (w, h):
            rgb = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        im = Image.fromarray(rgb)
        ph = ImageTk.PhotoImage(im)
        self.video_label.configure(image=ph)
        self.video_label.image = ph

    @staticmethod
    def _fmt(frames):
        try:
            s = int(frames) // 1
        except Exception:
            s = 0
        s = int(s)
        m, sec = divmod(s, 60)
        return f"{m:02d}:{sec:02d}"


# =========================================================================
# =========================================================================
# 使用说明 / 免责声明 弹窗
# =========================================================================
USAGE_TEXT = """【DJI 实时视觉识别 · 使用说明】

一、第一次打开
1. 双击桌面「DJI视觉识别.bat」启动（已配 GPU 版，识别吃显卡）。
2. 首页点「开启直播」连上无人机推流；或点「播放本地文件」选视频。
3. 顶部「识别」默认开启，画面会自动框出电线杆 / 鸟 / 高铁等目标。

二、常用操作
· 识别开关：关掉就是纯播放器，只看画面、不做识别。
· 精度：high 更准但慢，fast 更快。
· 追踪：给目标加稳定编号（●LOCK #id）。
· 自动挡：自动切换模型档位。
· 灵敏度：滑块越右越严格（只认更确定的目标），越左越灵敏。
· 挂件：把识别画面作为一个小窗浮在别的软件上面。
· 音乐：内置音乐台，可搜歌、看双语歌词。
· LUT：给画面调色，浓度可调。

三、关闭识别 = 纯 RTMP 播放器
关闭「识别」后，程序只播放视频流，不做任何识别、不加任何框、
不限制播放，按画面原本的元素播放（即最初版本的行为）。

四、注意事项
· 识别依赖显卡（RTX 3050 Ti），首次加载模型需几秒。
· 直播需无人机先推流到设定地址。
· 关闭「识别」时，追踪 / 自动挡会一并关闭并禁用。

（下滑查看免责声明）
────────────────────────────
【免责声明】
1. 本软件所用的 API 系从其他来源拷贝而来，其原始出处并非合规授权，
   仅供个人学习与研究使用，请勿用于商业或违规用途。
2. 软件内图片素材取自百度百科，已保留其原始标注、未作裁剪，
   版权归原作者 / 百度百科所有。
3. 本软件按「现状」提供，作者不对任何使用后果作担保。
4. 授权或问题事宜请联系作者邮箱：xuliuyang6@163.com
"""


def show_help(app):
    """弹出使用说明 + 免责声明窗口（首次强制 / 帮助按钮均可触发）。"""
    win = tk.Toplevel(app)
    win.title("使用说明 · 免责声明")
    win.geometry("660x580")
    win.transient(app)
    try:
        win.grab_set()
    except Exception:
        pass
    txt = tk.Text(win, wrap="word", font=("Microsoft YaHei", 10),
                   padx=14, pady=12, bg="#0e1830", fg="#e8eef7",
                   insertbackground="#22d3ee")
    txt.pack(fill="both", expand=True)
    txt.insert("1.0", USAGE_TEXT)
    txt.config(state="disabled")
    ttk.Button(win, text="我知道了", command=win.destroy).pack(pady=10)


# =========================================================================
# 统一应用容器
# =========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DJI 实时视觉识别")
        self.geometry("920x640")
        self.cfg_path = cfg_path_resolve()
        self.cfg = load_config(self.cfg_path)

        # 全局共享状态（首页设置，播放页使用 / 实时调整）
        mcfg = self.cfg.get("models", {})
        self.recognition_on = tk.BooleanVar(value=True)
        self.quality = tk.StringVar(value=mcfg.get("quality", "high"))
        self.tracking_on = tk.BooleanVar(value=mcfg.get("tracking", True))
        self.cascade_on = tk.BooleanVar(value=mcfg.get("auto_cascade", False))
        self.sensitivity = tk.DoubleVar(value=0.3)  # 识别灵敏度（置信度阈值），越低越灵敏
        self.rtmp_url = tk.StringVar(value="rtmp://192.168.10.2:1935/live/drone1")
        self.live_mode = tk.StringVar(value="client")  # client=连外部服务器 / server=本机作收流服务器
        self.rtmp_server = None  # 服务器模式下持有的 RtmpServer 实例
        self.file_path = tk.StringVar(value="")
        self.lut_conc = tk.IntVar(value=100)
        self.lut_selected = tk.StringVar(value="无")
        self.lut_cache = {"无": (None, 0), "内置·暖阳": lutmod.make_warm(17)}
        self.lut3d = None
        self.lut_size = 0
        self._lut_combos = []

        # 顶部常驻导航栏（首页 / 音乐）— 第三页入口
        self._build_topbar()

        self.container = tk.Frame(self)
        self.container.pack(fill="both", expand=True)

        # 音乐引擎单例：应用内本地播放（winmm），页面间共享、不打开浏览器
        self.music_engine = MusicEngine(self)

        self.player = None
        self.music = None
        self.home = HomePage(self.container, self)
        self.home.pack(fill="both", expand=True)

        # 首次打开强制弹出使用说明；之后通过顶部「帮助」按钮打开
        self._maybe_show_first_help()

    def _build_topbar(self):
        bar = ttk.Frame(self)
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="DJI 视觉识别",
                  font=("Microsoft YaHei", 11, "bold")).pack(side="left", padx=10)
        ttk.Button(bar, text="首页", command=self.show_home).pack(side="left", padx=4)
        ttk.Button(bar, text="📺 视频", command=self.show_video).pack(side="left", padx=4)
        ttk.Button(bar, text="🎵 音乐", command=self.show_music).pack(side="left", padx=4)
        ttk.Button(bar, text="帮助", command=self._open_help).pack(side="left", padx=4)
        ttk.Label(bar, text="").pack(side="left", fill="x", expand=True)
        return bar

    def _open_help(self):
        show_help(self)

    def _app_dir(self):
        """返回程序所在目录（源码=app.py 目录；exe=可执行文件目录）。"""
        import sys
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def _maybe_show_first_help(self):
        """首次启动强制弹使用说明；标记文件存在后不再弹。"""
        try:
            flag = os.path.join(self._app_dir(), ".seen_help")
            if os.path.exists(flag):
                return
            self.after(600, lambda: show_help(self))
            open(flag, "w", encoding="utf-8").close()
        except Exception:
            pass

    def _clear_pages(self, keep_player=True):
        # 音乐页：独立的 Edge 窗口控制器，无持续资源，直接销毁
        if self.music is not None:
            try:
                self.music.destroy()
            except Exception:
                pass
            self.music = None
        # 播放页（含 RTMP 收流服务器 / 识别进程）：【默认保留】，仅隐藏。
        # 这样在首页 / 音乐页之间切换时，正在进行的直播不会被掐断；
        # 只有当播放页自己的「◀ 返回首页」按钮显式调用 _back_cleanup 时，
        # 才会真正停止推流并销毁页面。
        if self.player is not None:
            if keep_player:
                try:
                    self.player.pack_forget()   # 隐藏但保留对象 / 线程 / RTMP 服务器
                except Exception:
                    pass
            else:
                try:
                    self.player._back_cleanup()
                except Exception:
                    pass
                try:
                    self.player.destroy()
                except Exception:
                    pass
                self.player = None
        self.home.pack_forget()

    def show_player(self, mode, target, server=None):
        # 若已有播放页且模式一致（直播 / 文件仍在后台运行），直接重新显示，
        # 不重建、不重启 RTMP 服务器——避免「切换页面就把推流掐断」。
        if self.player is not None and getattr(self.player, "_page_mode", None) == mode:
            if self.music is not None:
                try:
                    self.music.destroy()
                except Exception:
                    pass
                self.music = None
            try:
                self.home.pack_forget()
            except Exception:
                pass
            self.player.pack(fill="both", expand=True)
            return
        self._clear_pages(keep_player=False)
        self.player = PlayerPage(self.container, self, mode, target, server=server)
        self.player._page_mode = mode
        self.player._page_target = target
        self.player.pack(fill="both", expand=True)

    def show_music(self):
        self._clear_pages(keep_player=True)   # 保留后台直播，仅切到音乐页
        if self.music is None:
            self.music = MusicPage(self.container, self)
        self.music.pack(fill="both", expand=True)

    def show_home(self):
        self._clear_pages(keep_player=True)   # 保留后台直播，仅切到首页视图
        self.home.pack(fill="both", expand=True)

    def show_video(self):
        """切回正在进行的直播 / 本地播放（若仍在后台运行）。"""
        if self.player is None:
            self.show_home()
            return
        if self.player.winfo_viewable():
            return
        # 销毁可能的音乐页（独立 Edge 窗口），避免堆叠
        if self.music is not None:
            try:
                self.music.destroy()
            except Exception:
                pass
            self.music = None
        try:
            self.home.pack_forget()
        except Exception:
            pass
        self.player.pack(fill="both", expand=True)


# =========================================================================
# 入口
# =========================================================================
def main():
    # CUDA 环境变量（必须在 import torch 前设置，防 PyInstaller 下 CUDA context 不稳）
    os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    if getattr(sys, "frozen", False):
        try:
            meipass = sys._MEIPASS
            os.add_dll_directory(meipass)
            tl = os.path.join(meipass, "torch_lib")
            if os.path.isdir(tl):
                os.add_dll_directory(tl)
            os.chdir(meipass)
        except Exception:
            pass

    # 无界面自检模式
    if "--selftest" in sys.argv:
        img = sys.argv[sys.argv.index("--selftest") + 1] if len(sys.argv) > sys.argv.index("--selftest") + 1 else "test_dji_1280.jpg"
        cfg = load_config(cfg_path_resolve())
        det = MultiModelDetector(cfg.get("models", {}))
        frame = cv2.imread(img)
        if frame is None:
            print("[selftest] 无法读取图片:", img)
            return
        states = {"fps": FPSCounter(), "alarm": {}, "alerts": {}, "sighting": {}}
        dets, alarm_res, atrig, sighted, info = analyze_frame(frame, det, cfg, states)
        print(f"[selftest] 加载模型 DL={[n for n,_,_ in det.dl_specs]} "
              f"检测={len(dets)} 危险={alarm_res.get('danger') if alarm_res else None} 看见={sighted}")
        for d in dets:
            print(f"   - {d['label']} conf={d['conf']:.2f} {d['bbox']}")
        print(f"[selftest] 实际模型: {det.active_models()}")
        print("[selftest] OK")
        return

    # 挂件模式（独立进程）
    if "--overlay" in sys.argv:
        idx = sys.argv.index("--overlay")
        title = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "DJI"
        cfg = load_config(cfg_path_resolve())
        run_overlay(cfg, title)
        return

    try:
        app = App()
        app.mainloop()
    except Exception as e:
        try:
            messagebox.showerror("启动失败", f"{type(e).__name__}: {e}")
        except Exception:
            print("启动失败:", e)


if __name__ == "__main__":
    main()
