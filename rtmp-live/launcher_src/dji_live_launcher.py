#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DJI 本地直播启动器 —— 双击即用，内置播放器 + AI 分析口子。

功能:
  1. 一键启动内置 mediamtx.exe (RTMP 收流服务端)
  2. 自动探测本机局域网 IP, 给出 RC2 要填的 RTMP 推流地址
  3. 内置 OpenCV 视频播放器 (拉 RTSP 流, 在窗口里直接看, 不开浏览器)
  4. 预留 AI 分析钩子: 同目录放 analyzer.py 实现 analyze(frame)->frame 即可接入识别
  5. 一键停止
仅在本机/同网观看, 不推公网, 无需 ffmpeg。
"""
import os
import sys
import subprocess
import socket
import threading
import queue
import time
import importlib.util
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import cv2
from PIL import Image, ImageTk

# ---- 路径解析: 打包后 sys._MEIPASS, 否则用脚本目录 ----
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
MEDIAmtx = os.path.join(BASE_DIR, "mediamtx.exe")
if not os.path.exists(MEDIAmtx):
    _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    _alt = os.path.join(_exe_dir, "mediamtx.exe")
    if os.path.exists(_alt):
        MEDIAmtx = _alt
RUN_CWD = os.path.dirname(MEDIAmtx)
STREAM = "drone1"          # 流名 (与 RC2 填的 /live/drone1 对应)
RTMP_PORT = 1935
RTSP_PORT = 8554
DISPLAY_W = 480
DISPLAY_H = 270


def get_lan_ip():
    """返回本机在局域网的 IP (用于 RC2 填推流地址)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


class NoopAnalyzer:
    """默认无操作分析器。将来用 analyzer.py 覆盖。"""
    name = "无 (未加载 AI 模块)"

    def analyze(self, frame):
        return frame


def load_analyzer(search_dir):
    """尝试从 search_dir/analyzer.py 动态加载 AI 分析模块。

    模块需实现 analyze(frame: np.ndarray[BGR]) -> np.ndarray[BGR]。
    返回 (analyzer_object, loaded_bool)。失败/不存在返回 (NoopAnalyzer(), False)。
    """
    path = os.path.join(search_dir, "analyzer.py")
    if not os.path.exists(path):
        return NoopAnalyzer(), False
    try:
        spec = importlib.util.spec_from_file_location("user_analyzer", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not hasattr(mod, "analyze") or not callable(mod.analyze):
            return NoopAnalyzer(), False

        # 兼容两种写法: 模块级 analyze 函数, 或含 analyze 的类实例
        obj = mod if callable(getattr(mod, "analyze", None)) else None
        if obj is None:
            for v in vars(mod).values():
                if callable(v) and hasattr(v, "__call__"):
                    obj = v
                    break
        name = getattr(mod, "ANALYZER_NAME", "自定义 analyzer.py")

        class _Wrap:
            def __init__(self, fn, name):
                self.analyze = fn
                self.name = name

        return _Wrap(obj, name), True
    except Exception as e:
        print("analyzer load error:", e)
        return NoopAnalyzer(), False


class LiveLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DJI 本地直播启动器  ·  v2.0 (内置播放器)")
        self.geometry("760x700")
        self.resizable(False, False)
        self.mtx_proc = None
        self.cap = None
        self.reader_thread = None
        self.display_timer = None
        self.preview_running = False
        self.frame_q = queue.Queue(maxsize=1)

        # ---- AI 钩子: 优先 exe 同目录, 回退 _MEIPASS ----
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        self.analyzer, self.analyzer_loaded = load_analyzer(_exe_dir)
        if not self.analyzer_loaded:
            self.analyzer, self.analyzer_loaded = load_analyzer(BASE_DIR)

        self._build_ui()
        self._refresh_ip()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = dict(padx=12, pady=5)

        ttk.Label(self, text="DJI Air 3S / RC2 本地直播观看 (不推公网)",
                  font=("Microsoft YaHei", 13, "bold")).pack(anchor="w", **pad)

        info = ttk.Frame(self)
        info.pack(fill="x", **pad)
        ttk.Label(info, text="本机局域网 IP:").grid(row=0, column=0, sticky="w")
        self.ip_var = tk.StringVar(value="检测中…")
        ttk.Label(info, textvariable=self.ip_var, font=("Consolas", 11, "bold"),
                  foreground="#1a73e8").grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(info, text="刷新", command=self._refresh_ip, width=6).grid(row=0, column=2, padx=6)

        ttk.Label(self, text="① 遥控器 DJI Fly 直播 → 选 RTMP/自定义 → 填下面这个:",
                  font=("Microsoft YaHei", 10)).pack(anchor="w", **pad)
        self.rtmp_var = tk.StringVar(value="rtmp://<IP>:1935/live/drone1")
        addr = ttk.Entry(self, textvariable=self.rtmp_var, font=("Consolas", 11))
        addr.pack(fill="x", **pad)
        addr.bind("<FocusIn>", lambda e: addr.select_range(0, "end"))

        btn = ttk.Frame(self)
        btn.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btn, text="② 启动服务器", command=self.start_server, width=16)
        self.btn_start.grid(row=0, column=0, padx=4)
        self.btn_preview = ttk.Button(btn, text="③ 开始预览", command=self.toggle_preview, width=16)
        self.btn_preview.grid(row=0, column=1, padx=4)
        self.btn_stop = ttk.Button(btn, text="■ 停止服务器", command=self.stop_server, width=16)
        self.btn_stop.grid(row=0, column=2, padx=4)
        self.btn_stop.state(["disabled"])

        # 视频显示区 (黑底, 初始黑图)
        self._black = ImageTk.PhotoImage(Image.new("RGB", (DISPLAY_W, DISPLAY_H), (0, 0, 0)))
        self.video_label = tk.Label(self, image=self._black, bg="black", relief="sunken")
        self.video_label.pack(pady=6, ipadx=1, ipady=1)

        self.hint_var = tk.StringVar(value="点『③ 开始预览』, 并在 RC2 上开播后开始显示画面")
        ttk.Label(self, textvariable=self.hint_var, font=("Microsoft YaHei", 9),
                  foreground="#888").pack(anchor="w", **pad)

        # AI 状态
        self.ai_var = tk.StringVar(value=f"AI 分析模块: {self.analyzer.name}")
        ttk.Label(self, textvariable=self.ai_var, font=("Microsoft YaHei", 9),
                  foreground="#0a7d28" if self.analyzer_loaded else "#888").pack(anchor="w", **pad)

        self.status_var = tk.StringVar(value="就绪 · 未启动")
        self.status = ttk.Label(self, textvariable=self.status_var, foreground="#555")
        self.status.pack(anchor="w", **pad)

        ttk.Label(self, text="服务器日志:").pack(anchor="w", **pad)
        self.log = scrolledtext.ScrolledText(self, height=7, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, **pad)
        self.log.insert("end", "提示: 启动服务器 → RC2 开播 → 点『开始预览』即可窗口内看。\n")
        self.log.insert("end", "AI 口子: 在 exe 同目录放 analyzer.py (实现 analyze(frame)) 即接入识别。\n")

    def _refresh_ip(self):
        ip = get_lan_ip()
        self.ip_var.set(ip)
        self.rtmp_var.set(f"rtmp://{ip}:{RTMP_PORT}/live/{STREAM}")
        self.ip = ip

    # ---------- 服务器 ----------
    def start_server(self):
        if self.mtx_proc and self.mtx_proc.poll() is None:
            messagebox.showinfo("提示", "服务器已在运行。")
            return
        if not os.path.exists(MEDIAmtx):
            messagebox.showerror("错误", f"找不到 mediamtx.exe:\n{MEDIAmtx}")
            return
        try:
            self.mtx_proc = subprocess.Popen(
                [MEDIAmtx], cwd=RUN_CWD,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            messagebox.showerror("启动失败", str(e))
            return
        self.btn_start.state(["disabled"])
        self.btn_stop.state(["!disabled"])
        self.status_var.set("● 运行中 · 等待 RC2 推流…")
        self._log(f"[启动] mediamtx.exe PID={self.mtx_proc.pid}")
        threading.Thread(target=self._pump_log, daemon=True).start()

    def _pump_log(self):
        if not self.mtx_proc:
            return
        for raw in self.mtx_proc.stdout:
            line = raw.decode("utf-8", "ignore").rstrip()
            if line:
                self._log(line)
        self._log("[退出] mediamtx 进程已结束")

    # ---------- 预览 (内置播放器) ----------
    def toggle_preview(self):
        if self.preview_running:
            self.stop_preview()
        else:
            self.start_preview()

    def start_preview(self):
        if self.preview_running:
            return
        if not (self.mtx_proc and self.mtx_proc.poll() is None):
            self._log("[预览] 提示: 服务器尚未启动, 先点『② 启动服务器』")
        self.preview_running = True
        self.btn_preview.configure(text="③ 停止预览")
        self.hint_var.set("正在连接 RTSP 推流… (若长时间黑屏, 请确认 RC2 已开播)")
        self.status_var.set("● 预览中 · 等待 RC2 推流…")
        self.cap = cv2.VideoCapture(
            f"rtsp://localhost:{RTSP_PORT}/live/{STREAM}", cv2.CAP_FFMPEG)
        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()
        self._display()

    def _reader(self):
        while self.preview_running and self.cap is not None:
            if not self.cap.isOpened():
                self.cap.open(
                    f"rtsp://localhost:{RTSP_PORT}/live/{STREAM}", cv2.CAP_FFMPEG)
                time.sleep(1.0)
                continue
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.5)
                continue
            try:
                self.frame_q.put_nowait(frame)
            except queue.Full:
                pass
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def _display(self):
        if not self.preview_running:
            return
        try:
            frame = self.frame_q.get_nowait()
        except queue.Empty:
            frame = None
        if frame is not None:
            try:
                frame = self.analyzer.analyze(frame)
            except Exception as e:
                self._log(f"[AI] analyze 异常: {e}")
            self._render_frame(frame)
        self.display_timer = self.after(30, self._display)

    def _render_frame(self, frame):
        h, w = frame.shape[:2]
        scale = min(DISPLAY_W / w, DISPLAY_H / h, 1.0)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        img = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (nw, nh))
        im = Image.fromarray(img)
        imgtk = ImageTk.PhotoImage(image=im)
        # 居中贴到固定尺寸黑底: 用新图 (黑底 + 帧)
        canvas = Image.new("RGB", (DISPLAY_W, DISPLAY_H), (0, 0, 0))
        canvas.paste(im, ((DISPLAY_W - nw) // 2, (DISPLAY_H - nh) // 2))
        final = ImageTk.PhotoImage(image=canvas)
        self.video_label.configure(image=final)
        self.video_label.image = final
        self.hint_var.set("")

    def stop_preview(self):
        self.preview_running = False
        if self.display_timer:
            self.after_cancel(self.display_timer)
            self.display_timer = None
        if self.reader_thread and self.reader_thread.is_alive():
            self.reader_thread.join(timeout=2)
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.btn_preview.configure(text="③ 开始预览")
        self.video_label.configure(image=self._black)
        self.video_label.image = self._black
        self.hint_var.set("预览已停止")
        self.status_var.set("已停止预览")

    # ---------- 服务器停止 ----------
    def stop_server(self):
        self.stop_preview()
        if self.mtx_proc and self.mtx_proc.poll() is None:
            try:
                self.mtx_proc.terminate()
                self._log("[停止] 已发送终止信号")
            except Exception as e:
                self._log(f"[停止] 错误 {e}")
        self.btn_start.state(["!disabled"])
        self.btn_stop.state(["disabled"])
        self.status_var.set("已停止")

    def on_close(self):
        self.stop_server()
        self.destroy()

    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")


if __name__ == "__main__":
    LiveLauncher().mainloop()
