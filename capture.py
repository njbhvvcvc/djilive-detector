"""
画面采集模块
支持三种取流方式：
  - window : 按窗口标题抓取（针对 DJILive.exe，使用 dxcam 低延迟 DirectX 抓取）
  - monitor: 抓取整块显示器
  - region : 抓取固定矩形区域
dxcam 不可用时自动回退到 mss（纯 Python 屏幕截图）。
"""
import time
import numpy as np

try:
    import dxcam
    HAS_DXCAM = True
except Exception:
    HAS_DXCAM = False

try:
    from mss import mss
    HAS_MSS = True
except Exception:
    HAS_MSS = False

try:
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.windll.user32
    _EnumWindows = _user32.EnumWindows
    _EnumWindows.restype = wintypes.BOOL
    _EnumWindows.argtypes = [wintypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    _GetWindowTextW = _user32.GetWindowTextW
    _GetWindowTextW.restype = ctypes.c_int
    _GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _IsWindowVisible = _user32.IsWindowVisible
    _IsWindowVisible.restype = wintypes.BOOL
    _IsWindowVisible.argtypes = [wintypes.HWND]
    _GetWindowRect = _user32.GetWindowRect
    _GetWindowRect.restype = wintypes.BOOL
    _GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False


def find_window_hwnd(title_substring):
    """根据标题子串查找窗口句柄，返回 (hwnd, title) 或 None。"""
    if not HAS_WIN32:
        return None
    result = []

    @wintypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _):
        if not _IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        _GetWindowTextW(hwnd, buf, 512)
        title = buf.value or ""
        if title_substring.lower() in title.lower():
            result.append((hwnd, title))
        return True

    _EnumWindows(_enum, 0)
    if not result:
        return None
    # 优先选择标题更长的（更精确匹配）
    result.sort(key=lambda x: -len(x[1]))
    return result[0]


def get_window_rect(hwnd):
    """返回窗口矩形 (left, top, right, bottom) 或 None。"""
    if not HAS_WIN32 or hwnd is None:
        return None
    rect = wintypes.RECT()
    if not _GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


class Capturer:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = cfg.get("mode", "window")
        self.title = cfg.get("window_title", "DJI")
        self.monitor_idx = int(cfg.get("monitor", 1))
        self.region = cfg.get("region", [0, 0, 1280, 720])
        self.fmt = cfg.get("capture_format", "rgb").lower()
        self.cap_w = cfg.get("capture_width")
        self.cap_h = cfg.get("capture_height")
        self._cam = None
        self._mss = None
        self._backend = None
        self._init()

    def _init(self):
        # 优先 dxcam（GPU DirectX，延迟最低）
        if HAS_DXCAM:
            try:
                if self.mode == "window":
                    hwnd = None
                    if HAS_WIN32:
                        found = find_window_hwnd(self.title)
                        if found:
                            hwnd = found[0]
                            print(f"[capture] 找到窗口: '{found[1]}' (hwnd={hwnd})")
                    if hwnd is None:
                        print(f"[capture] 未找到标题含 '{self.title}' 的窗口，回退到主显示器抓取。")
                        self._cam = dxcam.create(output_idx=self.monitor_idx - 1)
                    else:
                        self._cam = dxcam.create(title=self.title)
                elif self.mode == "monitor":
                    self._cam = dxcam.create(output_idx=self.monitor_idx - 1)
                elif self.mode == "region":
                    l, t, r, b = self.region
                    self._cam = dxcam.create(output_idx=0, region=(l, t, r, b))
                if self._cam is not None:
                    try:
                        self._cam.start()
                    except Exception:
                        pass
                    self._backend = "dxcam"
                    print(f"[capture] 使用 dxcam 后端 ({self.mode})")
                    return
            except Exception as e:
                print(f"[capture] dxcam 初始化失败: {e}")

        # 回退 mss
        if HAS_MSS:
            self._mss = mss()
            self._backend = "mss"
            print(f"[capture] 使用 mss 后端 ({self.mode})")
            return

        raise RuntimeError("dxcam 与 mss 均不可用，无法采集画面。")

    def get_frame(self):
        """返回 BGR 格式的 numpy 帧；无新帧时返回 None。"""
        frame = None
        if self._backend == "dxcam" and self._cam is not None:
            f = self._cam.grab()
            if f is not None:
                # dxcam 返回 RGB
                frame = f[..., :3]
                if self.fmt == "rgb":
                    frame = np.ascontiguousarray(frame[:, :, ::-1])  # RGB->BGR
        elif self._backend == "mss" and self._mss is not None:
            if self.mode == "region":
                l, t, r, b = self.region
                monitor = {"left": l, "top": t, "width": r - l, "height": b - t}
            else:
                # 主显示器或整屏
                mon = self._mss.monitors[self.monitor_idx] if self.mode == "monitor" else self._mss.monitors[1]
                monitor = mon
            shot = self._mss.grab(monitor)
            frame = np.array(shot)[:, :, :3]  # BGRA -> BGR

        if frame is None:
            return None

        # 限制抓取分辨率（降低推理负担）
        if self.cap_w and self.cap_h:
            h, w = frame.shape[:2]
            if w > self.cap_w or h > self.cap_h:
                frame = cv_resize(frame, self.cap_w, self.cap_h)
        return frame

    def release(self):
        try:
            if self._cam is not None:
                self._cam.stop()
        except Exception:
            pass


def cv_resize(frame, w, h):
    import cv2
    scale = min(w / frame.shape[1], h / frame.shape[0])
    if scale >= 1.0:
        return frame
    return cv2.resize(frame, (int(frame.shape[1] * scale), int(frame.shape[0] * scale)),
                      interpolation=cv2.INTER_AREA)


if __name__ == "__main__":
    cap = Capturer({"mode": "monitor", "monitor": 1})
    for _ in range(30):
        f = cap.get_frame()
        if f is not None:
            print("frame shape:", f.shape, "backend:", cap._backend)
            break
        time.sleep(0.1)
    cap.release()
