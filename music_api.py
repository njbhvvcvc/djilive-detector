# -*- coding: utf-8 -*-
"""
GD 音乐台 API 客户端（Python 版，对应 music-player.html 的 JS 逻辑）
====================================================================
- 数据来自 https://music-api.gdstudio.xyz （公开 API，仅供学习）
- 支持：搜索 / 按专辑搜 / 按歌手搜、解析播放直链、歌词(双语)、封面
- 本地缓存：歌词(music_lyrics.json) / 封面 / 音频(按 source:id 缓存)
- 可选 CORS 代理前缀（proxy）
镜像网页中的 apiURL / fetchJSON / normList / normTrack / parseLRC / buildLRC 等。
"""
import os
import re
import json
import time
import threading

import requests
from music_engine import LRCParser

API = "https://music-api.gdstudio.xyz/api.php"

# 浏览器式请求头（否则 API 返回 403）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36",
    "Referer": "https://music-api.gdstudio.xyz/",
    "Accept": "application/json, */*",
}

# 快捷歌手 / 专辑（与网页一致）
PRESET_ARTISTS = [
    {"label": "ずっと真夜中でいいのに。(ZUTOMAYO)", "q": "ずっと真夜中でいいのに。"},
    {"label": "ヨルシカ (Yorushika / 夜鹿)", "q": "Yorushika"},
    {"label": "ONE OK ROCK", "q": "ONE OK ROCK"},
    {"label": "imase (イマセ)", "q": "imase"},
    {"label": "東京スカパラダイスオーケストラ (Skapara)", "q": "東京スカパラダイスオーケストラ"},
]
PRESET_ALBUMS = [
    {"label": "沈香学（ZUTOMAYO）", "q": "沈香学"},
    {"label": "潜潜話（ZUTOMAYO）", "q": "潜潜話"},
    {"label": "エルマ / Elma（ヨルシカ）", "q": "エルマ"},
    {"label": "だから僕は音楽を辞めた（ヨルシカ）", "q": "だから僕は音楽を辞めた"},
    {"label": "Luxury Disease（ONE OK ROCK）", "q": "Luxury Disease"},
    {"label": "Ambitions（ONE OK ROCK）", "q": "Ambitions"},
    {"label": "凡才 / Bonsai（imase）", "q": "凡才"},
    {"label": "POP CUBE（imase）", "q": "POP CUBE"},
    {"label": "Ska Me Forever（Skapara）", "q": "Ska Me Forever"},
    {"label": "Paradise Blue（Skapara）", "q": "Paradise Blue"},
]

_BASE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_BASE, "music_cache")
LYRIC_CACHE = os.path.join(_BASE, "music_lyrics.json")

_lock = threading.Lock()


# ----------------------------------------------------------------------
# 基础请求
# ----------------------------------------------------------------------
def _ensure_dir():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        pass


def api_url(params, proxy=""):
    """拼接口地址；若设置 proxy 前缀则把完整 URL 编码后拼接。"""
    from urllib.parse import urlencode, quote
    qs = urlencode(params)
    full = API + "?" + qs
    if proxy:
        return proxy + quote(full, safe="")
    return full


def fetch_json(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception as e:
        raise RuntimeError("网络请求失败（可能为跨域/CORS 限制）：" + str(e))
    if r.status_code != 200:
        raise RuntimeError("HTTP " + str(r.status_code))
    try:
        return r.json()
    except Exception:
        raise RuntimeError("接口返回非 JSON 数据")


# ----------------------------------------------------------------------
# 数据规范化（对应网页 normList / normTrack）
# ----------------------------------------------------------------------
def norm_list(data):
    if isinstance(data, list):
        return data
    if not data:
        return []
    for key in ("data", "result", "list", "songs"):
        if isinstance(data.get(key), list):
            return data[key]
    if isinstance(data.get("data", {}).get("list"), list):
        return data["data"]["list"]
    return []


def norm_track(t, source):
    if not t:
        return None
    artist = t.get("artist")
    if isinstance(artist, list):
        artist = " / ".join(artist)
    elif artist is None:
        artist = ""
    s = str(t.get("source", source) or source).replace("_album", "")
    tid = t.get("id")
    if tid is None:
        return None
    tid = str(tid)
    return {
        "id": tid,
        "name": t.get("name") or t.get("title") or "未知歌曲",
        "artist": str(artist),
        "album": t.get("album") or t.get("albumname") or "",
        "pic_id": str(t["pic_id"]) if t.get("pic_id") is not None else "",
        "lyric_id": str(t.get("lyric_id") if t.get("lyric_id") is not None else tid),
        "source": s,
    }


def pic_url(track, size=500):
    if not track.get("pic_id"):
        return ""
    return api_url({"types": "pic", "source": track["source"],
                    "id": track["pic_id"], "size": size})


def resolve_url(track, br="320", proxy=""):
    u = api_url({"types": "url", "source": track["source"],
                 "id": track["id"], "br": br}, proxy)
    d = fetch_json(u)
    if isinstance(d, dict) and d.get("url"):
        return d["url"]
    raise RuntimeError("该音源未返回可播放链接")


def search_raw(name, source="netease", album=False, pages=1,
               count=20, br="320", proxy="", _retries=1):
    src = source + ("_album" if album else "")
    items = []
    for p in range(1, pages + 1):
        arr = []
        for attempt in range(_retries + 1):
            try:
                u = api_url({"types": "search", "source": src, "name": name,
                             "count": count, "pages": p}, proxy)
                d = fetch_json(u)
                arr = [norm_track(t, source) for t in norm_list(d)]
                arr = [x for x in arr if x]
                if arr or attempt == _retries:
                    break
            except Exception:
                if attempt == _retries:
                    break
            time.sleep(1.0)
        if not arr:
            break
        items += arr
        if len(arr) < count:
            break
    return items


# ----------------------------------------------------------------------
# 歌词（对应网页 fetchLyric / cacheLyric / buildLRC / parseLRC）
# ----------------------------------------------------------------------
def _load_lyric_cache():
    try:
        with open(LYRIC_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_lyric_cache(cache):
    try:
        with open(LYRIC_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def get_cached_lyric(track):
    cache = _load_lyric_cache()
    return cache.get(f"{track['source']}:{track['lyric_id']}")


def cache_lyric(track, raw):
    payload = {
        "source": track["source"],
        "id": track["lyric_id"],
        "name": track["name"],
        "artist": track["artist"],
        "album": track["album"],
        "lyric": raw.get("lyric", ""),
        "tlyric": raw.get("tlyric", ""),
        "savedAt": int(time.time() * 1000),
    }
    cache = _load_lyric_cache()
    cache[f"{track['source']}:{track['lyric_id']}"] = payload
    _save_lyric_cache(cache)
    return payload


def fetch_lyric(track, proxy=""):
    u = api_url({"types": "lyric", "source": track["source"],
                 "id": track["lyric_id"]}, proxy)
    try:
        d = fetch_json(u)
    except Exception:
        return {"lyric": "", "tlyric": ""}
    if isinstance(d, dict):
        return {"lyric": d.get("lyric", ""), "tlyric": d.get("tlyric", "")}
    return {"lyric": "", "tlyric": ""}


def build_lrc(meta):
    """合并原文 + 译文为标准双语 .lrc 文本（对应网页 buildLRC）。"""
    def fmt(sec):
        m = int(sec // 60)
        s = int(sec % 60)
        ms = round((sec - int(sec)) * 100)
        return f"{m:02d}:{s:02d}.{ms:02d}"

    tmap = {}
    for t in LRCParser.parse(meta.get("tlyric", "")):
        tmap[round(t["time"] * 1000)] = t["text"]
    lines = []
    for o in LRCParser.parse(meta.get("lyric", "")):
        tr = tmap.get(round(o["time"] * 1000), "")
        lines.append("[" + fmt(o["time"]) + "]" + o["text"] +
                    (("\n[" + fmt(o["time"]) + "]" + tr) if tr else ""))
    head = [f"[ti:{meta.get('name', '')}]",
            f"[ar:{meta.get('artist', '')}]",
            f"[al:{meta.get('album', '')}]", ""]
    return "\n".join(head + lines)


def export_lrc(track, lrc_text, downloads_dir=None):
    if downloads_dir is None:
        try:
            downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        except Exception:
            downloads_dir = CACHE_DIR
    _ensure_dir()
    name = (f"{track['artist']} - {track['name']}.lrc")
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    try:
        with open(os.path.join(downloads_dir, name), "w", encoding="utf-8") as f:
            f.write(lrc_text)
    except Exception:
        pass


# ----------------------------------------------------------------------
# 封面 / 音频缓存
# ----------------------------------------------------------------------
def get_cover(track, size=500):
    """返回缓存封面图片的本地路径，失败返回 None。"""
    if not track.get("pic_id"):
        return None
    key = f"cover_{track['source']}_{track['pic_id']}_{size}"
    path = os.path.join(CACHE_DIR, key + ".jpg")
    if os.path.isfile(path):
        return path
    _ensure_dir()
    u = pic_url(track, size)
    if not u:
        return None
    try:
        r = requests.get(u, headers=HEADERS, timeout=15)
        if r.status_code == 200 and r.content:
            with open(path, "wb") as f:
                f.write(r.content)
            return path
    except Exception:
        pass
    return None


def get_audio(track, br="320", proxy=""):
    """解析播放直链并下载到本地缓存，返回本地文件路径。"""
    key = f"audio_{track['source']}_{track['id']}_{br}"
    path = os.path.join(CACHE_DIR, key + ".mp3")
    if os.path.isfile(path) and os.path.getsize(path) > 1024:
        return path
    _ensure_dir()
    url = resolve_url(track, br, proxy)
    r = requests.get(url, headers=HEADERS, timeout=60, stream=True)
    r.raise_for_status()
    ext = ".mp3"
    ct = r.headers.get("Content-Type", "")
    if "audio/mp4" in ct or "m4a" in url:
        ext = ".m4a"
    elif "audio/flac" in ct:
        ext = ".flac"
    path = os.path.join(CACHE_DIR, key + ext)
    with open(path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return path


if __name__ == "__main__":
    # 简易自测
    try:
        res = search_raw("Yorushika", count=2)
        print("search:", [(t["name"], t["artist"]) for t in res])
        if res:
            p = get_audio(res[0], br="128")
            print("audio cached:", p)
            ly = fetch_lyric(res[0])
            print("lyric len:", len(ly.get("lyric", "")))
    except Exception as e:
        print("self-test failed:", e)
