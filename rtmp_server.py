"""
RTMP 收流服务器（内置推流端）。

DJI 无人机 / OBS 等把 RTMP 流「推」到本机，本模块用 ffmpeg
（-rtmp_listen 1，监听模式）接收，直接把 H.264 解码为原始 BGR 像素，
通过 stdout 管道交给 Python（read_frame 按固定尺寸读取）。

为何走「管道原始帧」而非「UDP / OpenCV 解封装」：
  - 无关键帧依赖：OpenCV 读 UDP/MPEG-TS 必须等一个 IDR 才能解码，
    连接瞬间的首帧极易丢失 → 表现为「连上了但永远黑屏」。
  - 无 socket 顺序死锁：ffmpeg 先输入后输出，UDP 主动推送已绕开，
    而原始帧管道连 socket 都不需要。
  - 零丢失：ffmpeg 在无人机连上前阻塞不吐数据，连接后逐帧直出。
GPU 仍留给 YOLO 推理（解码走 CPU，本地环回带宽充足）。

依赖：imageio-ffmpeg（已通过国内源装好，wheel 自带 ffmpeg.exe）。
无需联网、无需系统安装 ffmpeg。
"""
import os
import sys
import subprocess
import time
import socket
import threading

import numpy as np
import imageio_ffmpeg as _ff


def _resolve_ffmpeg(explicit=None):
    """定位 ffmpeg 可执行文件。

    优先级（前面的命中即返回，避免无谓下载）：
      1) 显式传入
      2) 打包后 exe 同级 / 脚本同级 / PyInstaller 解压目录里的 ffmpeg.exe
      3) imageio_ffmpeg 自带二进制（仅当文件已存在时返回，不触发下载）
      4) 系统 PATH 中的 ffmpeg
    全部失败则抛出清晰的 FileNotFoundError（供上层弹窗提示，而非静默失败）。
    """
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"指定的 ffmpeg 不存在: {explicit}")

    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    # 待搜目录：exe 同级、脚本同级、PyInstaller 解压目录（onedir 下即 _internal）
    search_dirs = [exe_dir, here]
    if getattr(sys, "_MEIPASS", ""):
        search_dirs.append(sys._MEIPASS)
    # 先按裸名精确匹配
    for d in search_dirs:
        c = os.path.join(d, "ffmpeg.exe")
        if os.path.isfile(c):
            return c
    # 再按通配匹配（imageio_ffmpeg 自带文件名为 ffmpeg-win-x86_64-vX.exe）
    import glob as _glob
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        hits = sorted(_glob.glob(os.path.join(d, "ffmpeg*.exe")))
        if hits:
            return hits[0]

    # 命令行 venv 场景：imageio_ffmpeg 自带二进制（文件存在则直接返回，不下载）
    try:
        p = _ff.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass

    import shutil
    in_path = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    if in_path:
        return in_path

    raise FileNotFoundError(
        "找不到 ffmpeg 可执行文件。\n"
        "请确认 ffmpeg.exe 已与本程序放置在同一目录，"
        "或使用最新打包的 exe（已内置 ffmpeg）。")


def get_lan_ip():
    """返回本机在局域网里的 IP（用于告诉无人机往哪推）。
    不依赖外网连通性：国内无外网环境下连 8.8.8.8 会失败并退化成
    127.0.0.1（导致无人机往自己回环推、本机收不到流）。改为枚举本机
    网络接口 / 解析 ipconfig 提取真实私有 IP。"""
    import subprocess as _sp
    import re as _re
    # 方法1：Windows 解析 ipconfig（输出含中文，用 gbk 解码）
    try:
        out = _sp.check_output("ipconfig", shell=True,
                                encoding="gbk", errors="ignore")
        pat = _re.compile(r"IPv4\s*[^\d]*?(\d{1,3}(?:\.\d{1,3}){3})")
        # 优先私有网段
        for m in pat.finditer(out):
            ip = m.group(1)
            if ip.startswith(("192.168.", "10.", "172.16.", "172.17.",
                            "172.18.", "172.19.", "172.2", "172.30.", "172.31.")):
                return ip
        # 次选：任意非回环 / 非 APIPA(169.254) 地址
        for m in pat.finditer(out):
            ip = m.group(1)
            if not ip.startswith(("127.", "169.254.")):
                return ip
    except Exception:
        pass
    # 方法2：socket 兜底（无外网时会失败，仅最后才用）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


class RtmpServer:
    """
    监听 RTMP 推流的收流服务器。

    start()  启动 ffmpeg 监听进程，无人机推流至
             rtmp://<本机IP>:<listen_port>/<stream> 即可连入。
             收到流后，ffmpeg 把 H.264 解码为原始 BGR 像素，通过 stdout
             管道直接交给 Python（read_frame），绕开 UDP / 解封装 / 关键帧全部坑。
    read_frame() 从管道按固定尺寸读取一帧（numpy BGR），无帧时返回 None。
    stop()   结束 ffmpeg 进程。
    restart() 先停后起（断流自动重连时调用）。
    """

    def __init__(self, listen_port=1935, stream="live/drone1",
                 out_w=1280, out_h=720, app_ip="0.0.0.0",
                 ffmpeg_exe=None, local_port=1936):
        self.listen_port = listen_port
        self.stream = stream
        self.out_w = out_w
        self.out_h = out_h
        self.app_ip = app_ip
        # local_port 仅保留参数兼容（早期 UDP 方案使用，管道方案已不再需要）
        self.local_port = local_port
        # 解析 ffmpeg 路径：打包后从 exe 同级读取，命令行从 imageio_ffmpeg 读取
        self.ffmpeg_exe = _resolve_ffmpeg(ffmpeg_exe)
        print(f"[rtmp_server] ffmpeg 路径: {self.ffmpeg_exe}")
        self.proc = None
        self.running = False
        # 兼容旧引用（已不再作为 OpenCV 读取地址，仅保留属性）
        self.local_url = f"pipe://ffmpeg_stdout"

    def push_url(self):
        """无人机应填写的推流地址（含本机局域网 IP）。"""
        return f"rtmp://{get_lan_ip()}:{self.listen_port}/{self.stream}"

    def start(self):
        # ffmpeg 作为 RTMP「服务器」等待发布者（无人机）连入，并把
        # H.264 解码为原始 BGR 像素，通过 stdout 管道交给 Python：
        #   -rtmp_listen 1  -> 输入侧等待一个 RTMP 发布者（无人机推流）
        #   -an              -> 丢弃音频，只要视频
        #   -vf scale        -> 强制输出固定尺寸（rawvideo 无头，必须双方约定尺寸）
        #   -f rawvideo -pix_fmt bgr24 -> 输出裸 BGR 像素到 stdout 管道
        # 读取端用 read_frame() 按 out_w*out_h*3 字节直接解析，无关键帧依赖、
        # 无 UDP 丢包、无 socket 顺序死锁，连接前 ffmpeg 阻塞不吐数据 → 零丢失。
        # 低延迟收流：无人机推流帧率可能很高（实测见过 222fps），
        # 若不在解码端封顶，管道缓冲会瞬间堆积成 2~3 秒延迟。
        #   - -fflags +nobuffer+genpts / -flags low_delay  -> 尽量不缓存输入
        #   - -probesize/-analyzeduration 调到极小            -> 秒级启动、不预缓冲
        #   - -rtbufsize 100k                            -> 收流缓冲极小
        #   - -vf scale,fps=30                           -> 解码端强制封顶 30fps
        #     （直接丢多余帧，满足「帧率限制为 30fps 避免 GPU 重复计算」，
        #      同时杜绝管道被高帧率淹没导致的延迟堆积）
        #   - -flush_packets 1                           -> 每帧立即吐出，不等缓冲
        cmd = [
            self.ffmpeg_exe, "-hide_banner", "-loglevel", "info",
            "-nostdin",
            "-rtmp_listen", "1",
            "-fflags", "+nobuffer+genpts",
            "-flags", "low_delay",
            "-probesize", "4096", "-analyzeduration", "100000",
            "-rtbufsize", "100k",
            "-i", f"rtmp://{self.app_ip}:{self.listen_port}/{self.stream}",
            "-an",
            "-vf", f"scale={self.out_w}:{self.out_h},fps=30",
            "-flush_packets", "1",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-",  # stdout 管道
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            # 后台线程把 ffmpeg 日志实时打印，便于「推流失败」时定位原因。
            # 注意：stdout 必须是字节模式（read_frame 要按字节读原始帧），
            # 所以这里绝不能用 text=True（会让所有管道变文本，read_frame 拼接报错）。
            def _pump():
                if self.proc and self.proc.stderr:
                    for line in iter(self.proc.stderr.readline, b""):
                        try:
                            print("[ffmpeg]", line.decode("utf-8", "ignore").rstrip())
                        except Exception:
                            pass
            threading.Thread(target=_pump, daemon=True).start()
            time.sleep(0.8)  # 给 ffmpeg 一点时间完成 bind/listen
            self.running = self.proc.poll() is None
        except Exception as e:
            print(f"[rtmp_server] 启动失败: {e}")
            self.running = False
        if not self.running:
            print(f"[rtmp_server] 未能启动（端口 {self.listen_port} 可能被占用，或 ffmpeg 参数错误）")
        return self.running

    def read_frame(self):
        """从 stdout 管道读取一帧原始 BGR 像素。

        返回 numpy(H,W,3) BGR 帧；无数据 / 进程结束 / EOF 时返回 None。
        """
        if self.proc is None or self.proc.poll() is not None:
            return None
        n = self.out_w * self.out_h * 3
        buf = b""
        try:
            while len(buf) < n:
                chunk = self.proc.stdout.read(n - len(buf))
                if not chunk:
                    return None  # EOF / 进程结束
                buf += chunk
        except Exception:
            return None
        return np.frombuffer(buf, dtype=np.uint8).reshape(self.out_h, self.out_w, 3)

    def stop(self):
        self.running = False
        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def restart(self):
        self.stop()
        time.sleep(0.3)
        return self.start()


if __name__ == "__main__":
    srv = RtmpServer()
    print("本机局域网 IP:", get_lan_ip())
    print("无人机推流地址:", srv.push_url())
    print("OpenCV 读取地址:", srv.local_url)
    if srv.start():
        print("服务器已启动，等待无人机推流…（Ctrl+C 退出）")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            srv.stop()
    else:
        print("启动失败。")
