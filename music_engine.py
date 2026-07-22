# -*- coding: utf-8 -*-
"""
音乐播放引擎（Windows 本地，无外部依赖）
============================================
用 ctypes 调用系统自带的 winmm.dll（mciSendString），直接播放本地
mp3 / wav / 其它 Windows 自带解码的音频文件，无需安装任何 pip 包、
也无需打开浏览器。完全在应用内运行。

- 支持：open / play / pause / resume / stop / seek / 音量 / 进度 / 时长
- 后台轮询线程在「主线程」(通过 root.after) 派发回调，保证 tkinter 线程安全
- 一首播完自动触发 on_end（UI 据此切下一首）

歌词解析（双语 LRC）见 LRCParser。
"""
import os
import re
import time
import threading
import ctypes
from ctypes import wintypes


# ----------------------------------------------------------------------
# 音频引擎
# ----------------------------------------------------------------------
class MusicEngine:
    def __init__(self, root):
        # root：tkinter 根窗口，用于把回调安全地派发回主线程
        self._root = root
        self._alias = "gdplayer"
        self._winmm = None
        self._opened = False
        self._playing = False
        self._paused = False
        self._length = 0
        self._volume = 900  # 0~1000
        self._lock = threading.Lock()
        self._poll = None
        self._stop_poll = threading.Event()
        # 当前曲目元信息
        self.current = None  # {path, name, artist, lyric_path, lyrics}
        # 当前双语歌词 [{time, orig, trans}]（由播放器通过 set_lyrics 注入）
        self._lyrics = []
        # 监听器：多个页面（音乐页 / 视频页）可同时订阅
        # 每个监听器需实现 on_music_tick / on_music_state /
        #            on_music_end / on_music_track（缺省忽略）
        self.listeners = []
        self._try_load_dll()

    # ---- 低层 mci ----
    def _try_load_dll(self):
        try:
            self._winmm = ctypes.windll.winmm
            self._winmm.mciSendStringW.argtypes = [
                wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]
            self._winmm.mciSendStringW.restype = wintypes.DWORD
            self._winmm.mciGetErrorStringW.argtypes = [
                wintypes.DWORD, wintypes.LPWSTR, wintypes.UINT]
            self._winmm.mciGetErrorStringW.restype = wintypes.BOOL
        except Exception as e:
            self._winmm = None
            print("[music] 加载 winmm.dll 失败:", e)

    def _send(self, cmd):
        if self._winmm is None:
            raise RuntimeError("winmm 不可用（非 Windows？）")
        buf = ctypes.create_unicode_buffer(1024)
        ret = self._winmm.mciSendStringW(cmd, buf, 1023, 0)
        if ret != 0:
            err = ctypes.create_unicode_buffer(256)
            self._winmm.mciGetErrorStringW(ret, err, 255)
            raise RuntimeError(f"mci({cmd}) -> {err.value}")
        return buf.value

    def _status(self, what):
        try:
            return self._send(f"status {self._alias} {what}")
        except Exception:
            return ""

    def _close_internal(self):
        if self._opened:
            try:
                self._send(f"stop {self._alias}")
            except Exception:
                pass
            try:
                self._send(f"close {self._alias}")
            except Exception:
                pass
            self._opened = False

    # ---- 公共 API ----
    def open(self, path):
        """打开并准备一个音频文件（不立即播放）。"""
        with self._lock:
            self._close_internal()
            last_err = None
            for typ in ("", "MPEGVideo", "MPEGVIDEO", "WaveAudio"):
                try:
                    cmd = f'open "{path}" alias {self._alias}'
                    if typ:
                        cmd += f" type {typ}"
                    self._send(cmd)
                    break
                except Exception as e:
                    last_err = e
                    continue
            else:
                raise RuntimeError(
                    f"无法打开音频（可能系统缺少解码器）：{last_err}")
            self._opened = True
            self._playing = False
            self._paused = False
            self._length = 0
            try:
                self._length = int(self._status("length") or 0)
            except Exception:
                self._length = 0
            try:
                self.set_volume(self._volume / 1000.0)
            except Exception:
                pass

    def add_listener(self, l):
        if l is not None and l not in self.listeners:
            self.listeners.append(l)

    def remove_listener(self, l):
        try:
            self.listeners.remove(l)
        except Exception:
            pass

    def load_and_play(self, path, name="", artist="", lyric_path=None):
        self.open(path)
        self.current = {"path": path, "name": name or os.path.basename(path),
                       "artist": artist or "", "lyric_path": lyric_path,
                       "lyrics": []}
        self._lyrics = []
        self._dispatch("on_music_track", self.current["name"], self.current["artist"])
        self.play()

    def play(self):
        with self._lock:
            if not self._opened:
                return
            self._send(f"play {self._alias}")
            self._playing = True
            self._paused = False
        self._start_poll()
        self._emit_state()

    def pause(self):
        if self._playing and not self._paused:
            try:
                self._send(f"pause {self._alias}")
            except Exception:
                pass
            self._paused = True
            self._emit_state()

    def resume(self):
        if self._paused:
            try:
                self._send(f"resume {self._alias}")
            except Exception:
                pass
            self._paused = False
            self._emit_state()

    def toggle(self):
        if not self._opened:
            return
        if self._paused:
            self.resume()
        else:
            self.pause()

    def stop(self):
        self._stop_poll.set()
        try:
            self._send(f"stop {self._alias}")
        except Exception:
            pass
        self._playing = False
        self._paused = False
        self._emit_state()

    def seek(self, ms):
        with self._lock:
            if not self._opened:
                return
            ms = max(0, int(ms))
            try:
                self._send(f"seek {self._alias} to {ms}")
            except Exception:
                pass

    def set_volume(self, v):  # v: 0~1
        self._volume = max(0, min(1000, int(v * 1000)))
        if self._opened:
            try:
                self._send(f"setaudio {self._alias} volume to {self._volume}")
            except Exception:
                pass

    def position(self):
        if not self._opened:
            return 0
        try:
            return int(self._status("position") or 0)
        except Exception:
            return 0

    def length(self):
        if self._length:
            return self._length
        try:
            self._length = int(self._status("length") or 0)
        except Exception:
            pass
        return self._length

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._paused

    # ---- 歌词广播 ----
    def set_lyrics(self, lines):
        """注入当前曲目的双语歌词，并广播给所有监听器（含音乐页/视频页）。"""
        self._lyrics = lines or []
        if self.current is not None:
            self.current["lyrics"] = self._lyrics
        self._dispatch("on_music_lyric", self._lyrics)

    # ---- 轮询 ----
    def _start_poll(self):
        if self._poll and self._poll.is_alive():
            return
        self._stop_poll.clear()
        self._poll = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll.start()

    def _poll_loop(self):
        while not self._stop_poll.is_set():
            time.sleep(0.25)
            if not self._opened or not self._playing:
                continue
            try:
                pos = self.position()
                ln = self.length()
                if ln and pos >= ln - 350:
                    # 播放结束
                    self._playing = False
                    self._dispatch("on_music_end")
                    self._emit_state()
                    break
                self._dispatch("on_music_tick", pos, ln)
            except Exception:
                pass

    # ---- 线程安全派发（主线程）----
    def _dispatch(self, method, *args):
        """把回调安全地派发到 tkinter 主线程，并广播给所有监听器。"""
        for l in list(self.listeners):
            fn = getattr(l, method, None)
            if not callable(fn):
                continue
            try:
                self._root.after(0, lambda f=fn, a=args: f(*a))
            except Exception:
                try:
                    fn(*args)
                except Exception:
                    pass

    def _emit_state(self):
        state = "stopped"
        if self._playing and not self._paused:
            state = "playing"
        elif self._paused:
            state = "paused"
        self._dispatch("on_music_state", state)


# ----------------------------------------------------------------------
# 歌词解析（双语 LRC）
# ----------------------------------------------------------------------
class LRCParser:
    TS_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")

    @staticmethod
    def parse(text):
        """解析单份 LRC 文本，返回 [{time:秒, text}]（已按时间排序）。"""
        out = []
        if not text:
            return out
        for line in text.splitlines():
            stamps = LRCParser.TS_RE.findall(line)
            if not stamps:
                continue
            content = LRCParser.TS_RE.sub("", line).strip()
            if not content:
                continue
            for mm, ss, xx in stamps:
                minute = int(mm)
                sec = int(ss)
                ms = int((xx + "000")[:3]) if xx else 0
                out.append({"time": minute * 60 + sec + ms / 1000.0,
                            "text": content})
        out.sort(key=lambda x: x["time"])
        return out

    @staticmethod
    def load_pair(orig_text, trans_text=None):
        """合并原文 + 译文为双语歌词列表 [{time, orig, trans}]。"""
        orig = LRCParser.parse(orig_text)
        tmap = {}
        if trans_text:
            for t in LRCParser.parse(trans_text):
                tmap[round(t["time"] * 100)] = t["text"]
        return [{"time": o["time"], "orig": o["text"],
                 "trans": tmap.get(round(o["time"] * 100), "")}
                for o in orig]

    @staticmethod
    def find_lyric(audio_path):
        """根据音频路径查找同名词库 .lrc（或 .tr.lrc 译文）。返回 (orig_path, trans_path)。"""
        base = os.path.splitext(audio_path)[0]
        orig = base + ".lrc"
        trans = base + ".tr.lrc"
        return (orig if os.path.isfile(orig) else None,
                trans if os.path.isfile(trans) else None)


if __name__ == "__main__":
    # 简单自测（不依赖 tkinter 也可验证解析）
    sample = ("[00:00.00]作词 : 某人\n"
              "[00:03.50]第一句歌词\n"
              "[00:07.00][00:20.00]重复的句\n")
    print("parse:", LRCParser.parse(sample))
    print("find:", LRCParser.find_lyric("C:/music/song.mp3"))
