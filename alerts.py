"""
自定义报警模块（识别出即触发）
==================================================================
与 alarm.py 的「危险靠近」(距离型)不同，这里是「存在型」报警：
只要画面里出现配置中指定的标签（如 高铁/火车），就立即触发：
  - 屏幕红色边框闪烁（blink，可按标签单独开关）
  - 底部自定义横幅文字（text，可自由写）
  - 可选 Windows 系统蜂鸣（beep）

配置见 config.yaml 的 `alerts:` 段。可任意增删 targets。
典型用法：识别出「高铁/火车」就全屏红闪，提醒飞手注意飞行安全。
"""
import math
import time

import cv2


# --------------------------------------------------------------------------
# 分析：本帧出现了哪些需要报警的标签
# --------------------------------------------------------------------------
def analyze(detections, cfg):
    """
    输入检测列表，返回被触发的报警列表：
      [ {label, text, color(bgr), blink, beep}, ... ]
    未配置或无障碍标签 -> 返回空列表。
    """
    a = cfg or {}
    if not a.get("enabled", True):
        return []
    targets = a.get("targets", [])
    if not targets:
        return []
    labels_present = {d["label"] for d in detections}
    triggered = []
    for t in targets:
        lab = t.get("label")
        if lab in labels_present:
            triggered.append({
                "label": lab,
                "text": t.get("text", f"检测到{lab}"),
                "color": tuple(int(c) for c in t.get("color", (0, 0, 255))),
                "blink": bool(t.get("blink", True)),
                "beep": bool(t.get("beep", False)),
            })
    return triggered


# --------------------------------------------------------------------------
# 绘制：全屏红闪边框 + 底部横幅
# --------------------------------------------------------------------------
def draw(frame, triggered, cfg=None, t=None):
    """
    在 frame 上叠加闪烁红框 + 横幅（就地修改，返回 frame）。
    triggered 为空则原样返回。
    """
    if not triggered:
        return frame
    cfg = cfg or {}
    hz = float(cfg.get("blink_hz", 2.0))
    thick = int(cfg.get("border_thickness", 14))
    t = time.time() if t is None else t

    # 主色取最后一个被触发的报警（一般就是红）
    main = triggered[-1]
    color = main["color"]
    blink = any(x["blink"] for x in triggered)

    # 闪烁强度 0.35 ~ 1.0（约 hz 次/秒）
    if blink:
        intensity = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * hz * 2.0 * math.pi))
    else:
        intensity = 0.9

    h, w = frame.shape[:2]

    # 1) 全屏红色边框（闪烁）
    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (w - 1, h - 1), color, thick)
    cv2.addWeighted(ov, intensity, frame, 1 - intensity, 0, frame)

    # 2) 底部横幅
    texts = "  |  ".join(x["text"] for x in triggered)
    bh = 42
    bo = frame.copy()
    cv2.rectangle(bo, (0, h - bh), (w, h), color, -1)
    cv2.addWeighted(bo, 0.82, frame, 0.18, 0, frame)

    # 3) 横幅文字（自动缩放避免溢出）
    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = 0.85
    (tw, th), _ = cv2.getTextSize(texts, font, fs, 2)
    while tw > w - 24 and fs > 0.3:
        fs -= 0.1
        (tw, th), _ = cv2.getTextSize(texts, font, fs, 2)
    cv2.putText(frame, texts, (14, h - bh + 30), font, fs,
                (255, 255, 255), 2, cv2.LINE_AA)
    return frame


# --------------------------------------------------------------------------
# 可选：Windows 系统蜂鸣（任一触发项 beep=true 且过冷却时响一次）
# --------------------------------------------------------------------------
def maybe_beep(triggered, cfg, state):
    if not triggered:
        state["was"] = False
        return
    any_beep = any(x["beep"] for x in triggered)
    now = time.time()
    cooldown = float(cfg.get("beep_cooldown", 1.5)) if cfg else 1.5
    was = state.get("was", False)
    last = state.get("last", 0.0)
    if any_beep and (not was) and (now - last > cooldown):
        try:
            import ctypes
            if hasattr(ctypes, "windll"):
                ctypes.windll.kernel32.Beep(880, 150)
            state["last"] = now
        except Exception:
            pass
    state["was"] = any_beep
