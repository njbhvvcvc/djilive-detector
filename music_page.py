# -*- coding: utf-8 -*-
"""
第三页：GD 音乐台（在线）

策略：把参考网页 music_player.html 用系统自带的 Microsoft Edge
以 --app 模式（干净的"应用窗口"，无地址栏/标签页）打开。
网页本体 100% 真实渲染，1:1。
- 本地起一个静态文件服务（127.0.0.1），避免 file:// 源被 Edge 限制 localStorage
- msedge 在独立进程打开，关闭互不影响主程序
- 若找不到 msedge，回退到系统默认浏览器
"""
import os
import sys
import socket
import threading
import functools
import subprocess
import socketserver
import http.server
import tkinter as tk
from tkinter import messagebox

WIN_TITLE = "GD 音乐台"
HTML_FILE = "music_player.html"


# ----------------------------------------------------------------- #
# 工具：定位 music_player.html (源码 vs PyInstaller 打包)
# ----------------------------------------------------------------- #
def _resource_base():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------- #
# 本地静态文件服务（解决 file:// 源被 Edge 限制 localStorage 的问题）
# ----------------------------------------------------------------- #
class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        pass


def _find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_local_server():
    """启动一个后台静态文件服务，返回 (server, port)"""
    base = _resource_base()
    if not os.path.isfile(os.path.join(base, HTML_FILE)):
        raise FileNotFoundError("找不到 %s（资源目录：%s）" % (HTML_FILE, base))
    handler = functools.partial(_SilentHandler, directory=base)
    port = _find_free_port()
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


# ----------------------------------------------------------------- #
# 定位系统 Edge
# ----------------------------------------------------------------- #
def _find_msedge():
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge Core\msedge.exe",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 注册表
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
        ) as k:
            v = winreg.QueryValue(k, None)
            if v and os.path.isfile(v):
                return v
    except Exception:
        pass
    # PATH
    try:
        import shutil
        p = shutil.which("msedge.exe") or shutil.which("msedge")
        if p:
            return p
    except Exception:
        pass
    return None


# ----------------------------------------------------------------- #
# 启动 Edge（--app 模式）加载本地服务上的网页
# ----------------------------------------------------------------- #
def _launch_edge(url):
    """返回 (proc_or_None, used_msedge: bool)"""
    exe = _find_msedge()
    if exe:
        proc = subprocess.Popen(
            [exe, "--app=" + url, "--new-window", "--no-first-run",
             "--disable-features=msEdgeShowFeatureToast"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008,  # DETACHED_PROCESS：独立进程
        )
        return proc, True
    # 回退：默认浏览器
    try:
        import webbrowser
        webbrowser.open(url)
        return None, False
    except Exception as e:
        raise RuntimeError("找不到 Edge 且无法打开默认浏览器：%s" % e)


# ----------------------------------------------------------------- #
# 第三页：Tkinter 控制台（控制独立 Edge 窗口）
# ----------------------------------------------------------------- #
class MusicPage(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg="#0f1115")
        self.app = app
        self._alive = True
        self._proc = None
        self._server = None
        self._used_msedge = False
        self._build_ui()
        self.after(200, self._open_window)

    # ---------- UI ---------- #
    def _build_ui(self):
        # 顶栏
        top = tk.Frame(self, bg="#1a1d29", height=64)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="🎵  GD 音乐台（在线）",
                  bg="#1a1d29", fg="#e6e8ef",
                  font=("Microsoft YaHei", 14, "bold")
                  ).pack(side="left", padx=20)
        self.status_lbl = tk.Label(top, text="状态：准备启动…",
                                  bg="#1a1d29", fg="#8a8fa3",
                                  font=("Microsoft YaHei", 11))
        self.status_lbl.pack(side="right", padx=20)

        # 中部说明区
        body = tk.Frame(self, bg="#0f1115")
        body.pack(side="top", fill="both", expand=True)
        card = tk.Frame(body, bg="#1a1d29", highlightbackground="#2a2f45",
                        highlightthickness=1)
        card.place(relx=0.5, rely=0.5, anchor="center", width=560, height=320)

        tk.Label(card, text="🎶  在线音乐播放器（GD 音乐台）",
                 bg="#1a1d29", fg="#e6e8ef",
                 font=("Microsoft YaHei", 16, "bold")
                 ).pack(pady=(24, 8))
        tk.Label(card,
                 text="点击下方按钮在独立窗口中打开音乐播放器\n"
                      "（用系统 Edge 以应用模式打开，即真实网页）。\n"
                      "关闭音乐窗口不会影响视频识别和本地音乐播放。\n"
                      "需要联网访问 music-api.gdstudio.xyz。",
                 bg="#1a1d29", fg="#a8aec0",
                 font=("Microsoft YaHei", 11),
                 justify="center").pack(pady=(0, 18))

        # 按钮行
        row = tk.Frame(card, bg="#1a1d29")
        row.pack(pady=6)
        self.btn_open = tk.Button(row, text="🚀  打开音乐窗口",
                                  command=self._open_window,
                                  bg="#6366f1", fg="white",
                                  activebackground="#7c7ff5",
                                  activeforeground="white",
                                  font=("Microsoft YaHei", 12, "bold"),
                                  relief="flat", cursor="hand2",
                                  padx=22, pady=10)
        self.btn_open.pack(side="left", padx=6)
        self.btn_close = tk.Button(row, text="✖  关闭音乐窗口",
                                  command=self._close_window,
                                  bg="#3a3f5c", fg="#e6e8ef",
                                  activebackground="#4a4f6c",
                                  activeforeground="white",
                                  font=("Microsoft YaHei", 12),
                                  relief="flat", cursor="hand2",
                                  padx=22, pady=10)
        self.btn_close.pack(side="left", padx=6)

        # 底部提示
        tk.Label(self, text="提示：视频页底部仍可使用本地音乐播放（不依赖此功能）。",
                 bg="#0f1115", fg="#6a6f83",
                 font=("Microsoft YaHei", 10)).pack(side="bottom", pady=12)

    # ---------- 控制 ---------- #
    def _set_status(self, msg, color="#8a8fa3"):
        if not self._alive:
            return
        try:
            self.status_lbl.config(text="状态：" + msg, fg=color)
        except Exception:
            pass

    def _open_window(self):
        if not self._alive:
            return
        # 如果已经在跑
        if self._proc and self._proc.poll() is None:
            self._set_status("音乐窗口已在运行（PID %d）" % self._proc.pid,
                             color="#22c55e")
            return
        self._set_status("正在启动 Edge 应用窗口…")
        try:
            self._server, port = _start_local_server()
            url = "http://127.0.0.1:%d/%s" % (port, HTML_FILE)
            self._proc, self._used_msedge = _launch_edge(url)
        except Exception as e:
            self._set_status("启动失败：%s" % e, color="#ef4444")
            messagebox.showerror(
                "音乐页启动失败",
                "无法打开 GD 音乐台在线播放器。\n\n"
                "请确认：\n"
                "  • 系统已安装 Microsoft Edge\n"
                "  • 网络可访问 music-api.gdstudio.xyz\n"
                "  • 项目内 music_player.html 存在\n\n"
                "错误：%s" % e,
            )
            return
        if self._used_msedge:
            self._set_status("已用 Edge 打开音乐窗口", color="#22c55e")
        else:
            self._set_status("已用默认浏览器打开（未找到 Edge）",
                             color="#f59e0b")
        self.after(1500, self._poll)

    def _close_window(self):
        if not self._alive:
            return
        if not self._proc or self._proc.poll() is not None:
            self._set_status("音乐窗口未在运行", color="#8a8fa3")
            return
        try:
            self._proc.terminate()
        except Exception:
            pass
        self._set_status("已请求关闭音乐窗口", color="#f59e0b")

    def _poll(self):
        if not self._alive:
            return
        if self._proc and self._proc.poll() is not None:
            rc = self._proc.poll()
            self._set_status("音乐窗口已关闭（返回码 %s）" % rc,
                             color="#8a8fa3")
            self._proc = None
        try:
            self.after(1500, self._poll)
        except Exception:
            pass

    # ---------- 销毁 ---------- #
    def destroy(self):
        self._alive = False
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._server:
            try:
                self._server.shutdown()
            except Exception:
                pass
        super().destroy()
