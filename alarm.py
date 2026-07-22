"""
危险靠近预警模块（task ②）
==================================================================
两种触发方式：
  A) 距离型：某个「危险物」(如 鸟 / 无人机 / 人) 的检测框，与某个
     「带电体」(电线 / 电线杆 / 设备近电 / 车辆近电 / 吊臂近电 / 高风险靠近)
     的检测框 边缘距离 < 阈值  -> 预警。
         - 较近  -> warn  （黄，提醒）
         - 非常近 -> danger（红，危险）
  B) 直接型：深度学习模型本身输出了「高风险靠近 / 设备近电 / 车辆近电 /
     吊臂近电」等类别 -> 直接判为最高告警，无需距离判断。

无论当前是 CV 直线兜底模式 还是 深度学习模式，都生效。
配置文件见 config.yaml 的 `alarm:` 段。
"""
import math
import time

import cv2


# --------------------------------------------------------------------------
# 几何辅助
# --------------------------------------------------------------------------
def _box_center(b):
    x1, y1, x2, y2 = b
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


def box_edge_distance(b1, b2):
    """两框最近边缘的像素距离（两框内部相交时为 0）。"""
    ax1, ay1, ax2, ay2 = b1
    bx1, by1, bx2, by2 = b2
    dx = max(bx1 - ax2, 0, ax1 - bx2)
    dy = max(by1 - ay2, 0, ay1 - by2)
    return math.hypot(dx, dy)


# --------------------------------------------------------------------------
# 核心分析
# --------------------------------------------------------------------------
def analyze(detections, cfg):
    """
    输入检测列表，返回预警结果 dict：
      {
        danger: bool, warn: bool,
        banner_text: str, banner_color: (b,g,r),
        events: [ {hazard, target, dist, severity} ... ],
        direct: [ det ... ]        # 模型直接判危的框
      }
    """
    a = cfg or {}
    hazard_labels = set(a.get("hazard_labels", ["鸟", "无人机", "人"]))
    target_labels = set(a.get("target_labels",
        ["电线", "电线杆", "设备近电", "车辆近电", "吊臂近电", "高风险靠近"]))
    direct_danger = set(a.get("direct_danger_labels",
        ["高风险靠近", "设备近电", "车辆近电", "吊臂近电"]))
    warn_px = float(a.get("warn_px", 120))
    danger_px = float(a.get("danger_px", 50))

    hazards = [d for d in detections if d["label"] in hazard_labels]
    targets = [d for d in detections if d["label"] in target_labels]
    direct = [d for d in detections if d["label"] in direct_danger]

    events = []
    # 距离型：每个危险物找最近的带电体
    for h in hazards:
        best = None
        for t in targets:
            dist = box_edge_distance(h["bbox"], t["bbox"])
            if best is None or dist < best[1]:
                best = (t, dist)
        if best is None:
            continue
        t, dist = best
        if dist <= danger_px:
            sev = "danger"
        elif dist <= warn_px:
            sev = "warn"
        else:
            continue
        events.append({"hazard": h, "target": t, "dist": dist, "severity": sev})

    danger = bool(direct) or any(e["severity"] == "danger" for e in events)
    warn = (not danger) and (len(events) > 0)

    if danger:
        banner_text = "危险靠近预警！"
        banner_color = (0, 0, 255)        # 红
    elif warn:
        banner_text = "注意：目标靠近带电体"
        banner_color = (0, 215, 255)      # 黄
    else:
        banner_text = ""
        banner_color = (0, 255, 0)

    return {
        "danger": danger, "warn": warn,
        "banner_text": banner_text, "banner_color": banner_color,
        "events": events, "direct": direct,
    }


# --------------------------------------------------------------------------
# 绘制
# --------------------------------------------------------------------------
def draw_alarm(frame, result, cfg=None, blink_phase=0.0):
    """在 frame 上叠加预警标记（就地修改，返回 frame）。"""
    if not result:
        return frame
    cfg = cfg or {}
    show_links = cfg.get("show_links", True)

    # 直接危险框：红色实线 + “危”标
    for d in result.get("direct", []):
        x1, y1, x2, y2 = d["bbox"]
        _thick_rect(frame, (0, 0, 255), x1, y1, x2, y2, 3)
        cv2.putText(frame, "危", (int(x1), max(int(y1) - 8, 14)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)

    # 距离型事件：连线 + 距离 + 危险框描红
    for e in result.get("events", []):
        h, t, dist, sev = e["hazard"], e["target"], e["dist"], e["severity"]
        c = (0, 0, 255) if sev == "danger" else (0, 215, 255)
        if sev == "danger":
            _thick_rect(frame, c, *h["bbox"], 3)
        if show_links:
            ch = _box_center(h["bbox"])
            ct = _box_center(t["bbox"])
            cv2.line(frame, (int(ch[0]), int(ch[1])),
                     (int(ct[0]), int(ct[1])), c, 2, cv2.LINE_AA)
            mx, my = int((ch[0] + ct[0]) / 2), int((ch[1] + ct[1]) / 2)
            cv2.putText(frame, f"{dist:.0f}px", (mx, my),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, c, 2)

    # 顶部横幅（危险时脉冲闪烁）
    if result.get("banner_text"):
        _banner(frame, result["banner_text"], result["banner_color"],
                blink_phase, danger=result["danger"])
    return frame


def _thick_rect(frame, color, x1, y1, x2, y2, thick=2):
    cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), color, thick)


def _banner(frame, text, color, phase, danger=False):
    h, w = frame.shape[:2]
    bh = 34
    alpha = 0.85
    if danger:
        # 约 1Hz 脉冲：0.55 ~ 1.0
        alpha = 0.55 + 0.45 * (0.5 + 0.5 * math.sin(phase * math.pi * 2.0))
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, bh), color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)
    cv2.putText(frame, text, (12, 24), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (255, 255, 255), 2, cv2.LINE_AA)


# --------------------------------------------------------------------------
# 可选：Windows 系统蜂鸣（仅在 danger 上升沿且过冷却时响一次）
# --------------------------------------------------------------------------
def maybe_beep(result, cfg, state):
    if not cfg or not cfg.get("beep", False):
        return
    danger = result.get("danger", False)
    now = time.time()
    cooldown = float(cfg.get("beep_cooldown", 1.5))
    was = state.get("was_danger", False)
    last = state.get("last_beep", 0.0)
    if danger and (not was) and (now - last > cooldown):
        try:
            import ctypes
            if hasattr(ctypes, "windll"):
                ctypes.windll.kernel32.Beep(880, 120)
            state["last_beep"] = now
        except Exception:
            pass
    state["was_danger"] = danger
