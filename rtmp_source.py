"""
帧源模块：从 RTMP 推流地址或本地视频文件读取帧。
- RTMP：用 OpenCV 的 ffmpeg 后端直连（opencv_videoio_ffmpeg*.dll 已随 opencv 打包）。
- 本地文件：直接 cv2.VideoCapture(path)。
- 断流自动重连（RTMP 常见）；无法打开时可选回退到 ffmpeg 子进程拉流。
返回 BGR 帧（与 detector 一致）。
"""
import os
import subprocess
import time

import cv2
import numpy as np


class VideoSource:
    """统一帧源：rtmp / file / 本地摄像头。"""

    def __init__(self, url, kind=None, reconnect_delay=0.8, use_ffmpeg_subprocess=False,
                 realtime=True, pre_reconnect=None):
        self.url = url
        self.kind = kind or self._guess(url)
        self.reconnect_delay = reconnect_delay
        self.use_ffmpeg_subprocess = use_ffmpeg_subprocess
        self.realtime = realtime  # True=跳帧保实时 / False=逐帧处理
        self.pre_reconnect = pre_reconnect  # 断流重启钩子（如重启 RTMP 服务器）
        self.cap = None
        self._proc = None
        self._ff_w = None   # ffmpeg 子进程模式下的帧宽（_open_ffmpeg 探测）
        self._ff_h = None
        self._retries = 0
        self._max_retries = 50  # 约 40s 后放弃
        self._open()

    @staticmethod
    def _guess(url):
        u = (url or "").lower()
        if u.startswith("rtmp") or u.startswith("rtsp") or u.startswith("http"):
            return "stream"
        return "file"

    def _open(self):
        # 优先用 ffmpeg 后端（RTMP 兼容性最好）
        try:
            self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        except Exception:
            self.cap = None
        if self.cap is None or not self.cap.isOpened():
            try:
                self.cap = cv2.VideoCapture(self.url)
            except Exception:
                self.cap = None
        if self.cap is not None and self.cap.isOpened():
            # 降低 RTMP 缓冲，降低延迟
            try:
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            return
        # OpenCV 打不开（某些 RTMP 源）：回退 ffmpeg 子进程
        if self.use_ffmpeg_subprocess:
            self._open_ffmpeg()
        else:
            self.cap = None

    def _open_ffmpeg(self):
        """用系统 ffmpeg 子进程把 RTMP 拉成 rawvideo 喂给 stdout。
        先通过 ffprobe 探测宽高写进 _ff_w/_ff_h；探测失败则回退默认
        1280x720。修复原逻辑把 _ff_w 置 None 导致 _proc 模式永远读不到帧。
        """
        try:
            w, h = self._probe_ffmpeg_size()
            if not w or not h:
                w, h = 1280, 720
                print("[source] ffprobe 未返回尺寸，回退 1280x720")
            self._ff_w, self._ff_h = w, h
            self._proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 "-i", self.url,
                 "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**7)
        except Exception as e:
            print(f"[source] ffmpeg 子进程启动失败: {e}")
            self._proc = None

    def _probe_ffmpeg_size(self):
        """用 ffprobe 取视频流宽高；失败返回 (0,0)。"""
        try:
            out = subprocess.check_output(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", self.url],
                stderr=subprocess.DEVNULL, timeout=8)
            parts = out.decode("utf-8", "ignore").strip().split(",")
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return 0, 0

    def get_frame(self):
        """返回 BGR 帧；无帧/断流返回 None 并自动重连。
        直播模式下（RTMP/RTSP/HTTP）自动丢掉堆积帧，保证实时性。"""
        if self._proc is not None:
            # ffmpeg 子进程模式（尺寸已由 _open_ffmpeg 经 ffprobe 探测写定）
            if self._ff_w is None or self._ff_h is None:
                return None  # 尺寸未知（极少触发）
            raw = self._proc.stdout.read(self._ff_w * self._ff_h * 3)
            if not raw or len(raw) < self._ff_w * self._ff_h * 3:
                self._reconnect()
                return None
            return np.frombuffer(raw, dtype=np.uint8).reshape(self._ff_h, self._ff_w, 3)

        if self.cap is None or not self.cap.isOpened():
            self._reconnect()
            return None

        # 直播模式（非文件）+ 实时模式：跳过缓冲里堆积的旧帧，只取最新一帧（低延迟）。
        # 本地文件播放不走跳帧（否则视频加速播放）。
        if self.kind == "stream" and self.realtime:
            last = None
            for _ in range(8):  # 最多连读 8 帧，返回最后一帧（即最新可见帧）
                ok, frame = self.cap.read()
                if not ok or frame is None:
                    break  # 无更多立即可用帧 / 网络暂断
                last = frame
            if last is None:
                # 本轮连一帧都没读到 → 视为断开，触发重连逻辑
                self._reconnect()
                return None
            return last

        # 文件模式：正常顺序读取
        ok, frame = self.cap.read()
        if not ok or frame is None:
            self._reconnect()
            return None
        return frame

    def _reconnect(self):
        self._retries += 1
        if self._retries > self._max_retries:
            print("[source] 重连次数过多，放弃。")
            return
        print(f"[source] 帧源断开，重连 #{self._retries}…")
        # 断流重启钩子：例如 RTMP 服务器（ffmpeg 监听进程）随推流端
        # 断开而退出，需要在此先把它重新拉起，再重连 OpenCV。
        if callable(self.pre_reconnect):
            try:
                self.pre_reconnect()
            except Exception as e:
                print(f"[source] pre_reconnect 失败: {e}")
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass
        self.cap = None
        time.sleep(self.reconnect_delay)
        self._open()

    def release(self):
        try:
            if self.cap is not None:
                self.cap.release()
        except Exception:
            pass
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    import sys
    src = VideoSource(sys.argv[1] if len(sys.argv) > 1 else "test_dji_1280.jpg")
    for _ in range(50):
        f = src.get_frame()
        if f is not None:
            print("frame:", f.shape)
            break
        time.sleep(0.1)
    src.release()
