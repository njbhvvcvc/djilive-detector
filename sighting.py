"""
看见提醒模块（高铁 / 鸟等「看见即可、并非威胁」的目标）
==================================================================
和 alarm.py（危险靠近：红色脉冲横幅）以及 alerts.py（威胁：全屏红闪）不同，
这里针对「看见了就好、不是危险」的目标（如 高铁 / 鸟）：

  - 屏幕**顶部软提醒横幅**（青色，不红闪、不整屏红框）——「看见提醒」

默认【不自动拍照】。如需「看见就留一张图」，把 config.yaml 里
`sighting.capture` 改为 true 即可（见底部 maybe_capture）。
完全配置驱动，见 `config.yaml` 的 `sighting:` 段。可任意增删 targets。
典型用法：飞手看到高铁/鸟经过，系统顶部弹青色「看见提醒」横幅。
"""
import os
import time

import cv2
from datetime import datetime


# --------------------------------------------------------------------------
# 分析：本帧出现了哪些 sighting 目标（去重）
# --------------------------------------------------------------------------
def analyze(detections, cfg):
    """返回当前帧里、属于 sighting 目标的标签列表（去重）。"""
    a = cfg or {}
    if not a.get("enabled", True):
        return []
    targets = {t.get("label") for t in a.get("targets", []) if t.get("label")}
    if not targets:
        return []
    seen = []
    for d in detections:
        lab = d.get("label")
        if lab in targets and lab not in seen:
            seen.append(lab)
    return seen


# --------------------------------------------------------------------------
# 绘制：顶部软提醒横幅（青色，不红闪）
# --------------------------------------------------------------------------
def draw(frame, sighted, cfg=None, t=None):
    """在 frame 顶部画一条软提醒横幅（就地修改，返回 frame）。"""
    if not sighted:
        return frame
    cfg = cfg or {}
    color = tuple(int(c) for c in cfg.get("banner_color", [0, 255, 255]))
    text = "看见提醒：" + "、".join(sighted)
    h, w = frame.shape[:2]
    bh = 36
    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (w, bh), color, -1)
    cv2.addWeighted(ov, 0.82, frame, 0.18, 0, frame)
    # 青底配黑字更清晰（不抢危险的红色）
    cv2.putText(frame, text, (12, 26), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (0, 0, 0), 2, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------------
# 自动拍照（默认关闭，仅 config 里 capture=true 时生效）：
# 仅在「新出现」某目标时拍一张（事件触发，不每帧刷）
# --------------------------------------------------------------------------
# 标签 -> ASCII 文件名片段（避免中文/特殊字符导致 imwrite 写不出文件）
_TAG = {"高铁/火车": "train", "鸟": "bird", "人": "person", "无人机": "uav",
        "电线": "wire", "电线杆": "pole"}
def _tag(lab):
    return _TAG.get(lab, "obj")


def maybe_capture(frame, sighted, cfg, state):
    """
    仅在某个目标从「未出现」变为「出现」的瞬间拍一张。
    返回本次实际保存的文件路径列表。
    state 在帧间保持（记录上一帧已出现过哪些）。
    """
    prev = state.get("seen_prev", set())
    new = [lab for lab in sighted if lab not in prev]
    state["seen_prev"] = set(sighted)
    if not new or not cfg or not cfg.get("capture", False):
        return []
    cd = cfg.get("capture_dir", "captures/sighting")
    os.makedirs(cd, exist_ok=True)
    saved = []
    for lab in new:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = os.path.join(cd, f"sighting_{_tag(lab)}_{ts}.jpg")
        try:
            if cv2.imwrite(fn, frame):
                saved.append(fn)
        except Exception:
            pass
    return saved
