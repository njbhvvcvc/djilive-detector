"""
电线 / 电线杆 兜底检测（纯 OpenCV，零下载、实时极快）
原理：电线近似水平细直线，电线杆近似垂直结构。
用 Canny 边缘 + Hough 直线检测，按角度分桶：
  近水平(电线)  -> 按 y 聚类，每根画一个横跨框
  近垂直(电线杆) -> 按 x 聚类，每根画一个竖框
作为「无深度学习权重」时的可用兜底；若配置了 weights 且存在，detector 会优先用深度学习。
"""
import cv2
import numpy as np


def _cluster_1d(coords, gap):
    """把一维坐标按间距聚类，返回若干组。"""
    coords = sorted(coords)
    if not coords:
        return []
    groups, cur = [], [coords[0]]
    for c in coords[1:]:
        if c - cur[-1] <= gap:
            cur.append(c)
        else:
            groups.append(cur)
            cur = [c]
    groups.append(cur)
    return groups


def detect_lines(frame, cfg: dict):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, int(cfg.get("canny_low", 50)), int(cfg.get("canny_high", 150)))
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180,
        int(cfg.get("threshold", 80)),
        minLineLength=int(cfg.get("min_len", 40)),
        maxLineGap=int(cfg.get("max_gap", 10)),
    )
    if lines is None:
        return []

    h_segs, v_segs = [], []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            continue
        ang = abs(float(np.degrees(np.arctan2(dy, dx))))
        if ang < float(cfg.get("h_thr", 25)):        # 近水平 -> 电线
            h_segs.append((x1, y1, x2, y2))
        elif ang > float(cfg.get("v_thr", 65)):      # 近垂直 -> 电线杆
            v_segs.append((x1, y1, x2, y2))

    dets = []
    pad = int(cfg.get("pad", 4))
    wire_label = cfg.get("wire_label", "电线")
    pole_label = cfg.get("pole_label", "电线杆")
    wire_color = tuple(int(c) for c in cfg.get("wire_color", (0, 0, 255)))
    pole_color = tuple(int(c) for c in cfg.get("pole_color", (255, 0, 0)))
    gap = int(cfg.get("cluster_gap", 30))

    # 电线：按 y(中点) 聚类，每根一个横跨框
    if h_segs:
        ys = sorted({round((min(s[1], s[3]) + max(s[1], s[3])) / 2) for s in h_segs})
        for g in _cluster_1d(ys, gap):
            segs = [s for s in h_segs if round((min(s[1], s[3]) + max(s[1], s[3])) / 2) in g]
            xs = [s[0] for s in segs] + [s[2] for s in segs]
            ys_ = [s[1] for s in segs] + [s[3] for s in segs]
            dets.append({
                "bbox": (min(xs), min(ys_) - pad, max(xs), max(ys_) + pad),
                "label": wire_label, "color": wire_color, "conf": 0.5, "source": "cv",
            })

    # 电线杆：按 x(中点) 聚类，每根一个竖框
    if v_segs:
        xs = sorted({round((min(s[0], s[2]) + max(s[0], s[2])) / 2) for s in v_segs})
        for g in _cluster_1d(xs, gap):
            segs = [s for s in v_segs if round((min(s[0], s[2]) + max(s[0], s[2])) / 2) in g]
            xs_ = [s[0] for s in segs] + [s[2] for s in segs]
            ys_ = [s[1] for s in segs] + [s[3] for s in segs]
            dets.append({
                "bbox": (min(xs_) - pad, min(ys_), max(xs_) + pad, max(ys_)),
                "label": pole_label, "color": pole_color, "conf": 0.5, "source": "cv",
            })

    return dets
