"""绘制与 FPS 辅助函数。"""
import cv2
import time


def draw_detections(frame, detections, disp_cfg: dict):
    """在 frame 上绘制检测框与标签（就地修改）。
    如果 d 包含 "_predicted": True，用虚线半透明框表示预测位置。"""
    thick = int(disp_cfg.get("box_thickness", 2))
    fs = float(disp_cfg.get("font_scale", 0.5))
    show_conf = disp_cfg.get("show_conf", True)
    for d in detections:
        x1, y1, x2, y2 = d["bbox"]
        color = d["color"]
        predicted = d.get("_predicted", False)

        if predicted:
            # 预测框：虚线 + 半透明
            color_dim = tuple(max(c - 80, 0) for c in color)
            _draw_dashed(frame, (x1, y1), (x2, y2), color_dim, 1, 6)
            text = d["label"] + "~"
        else:
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, thick)
            text = d["label"]
            # 有追踪 ID 则显示（ByteTrack）
            if d.get("track_id") is not None:
                text += f"#{d['track_id']}"

        if show_conf and not predicted:
            text += f" {d['conf']:.2f}"
        # 标签底色块，保证可读
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, 1)
        ty = max(y1, th + 4)
        cv2.rectangle(frame, (x1, ty - th - 4), (x1 + tw + 4, ty), color, -1)
        cv2.putText(frame, text, (x1 + 2, ty - 2), cv2.FONT_HERSHEY_SIMPLEX,
                    fs, (0, 0, 0), 1, cv2.LINE_AA)
    return frame


def _draw_dashed(img, pt1, pt2, color, thickness=1, dash_len=6):
    """画虚线矩形框。"""
    x1, y1 = pt1
    x2, y2 = pt2
    # 上边
    for x in range(x1, x2, dash_len * 2):
        cv2.line(img, (x, y1), (min(x + dash_len, x2), y1), color, thickness)
    # 下边
    for x in range(x1, x2, dash_len * 2):
        cv2.line(img, (x, y2), (min(x + dash_len, x2), y2), color, thickness)
    # 左边
    for y in range(y1, y2, dash_len * 2):
        cv2.line(img, (x1, y), (x1, min(y + dash_len, y2)), color, thickness)
    # 右边
    for y in range(y1, y2, dash_len * 2):
        cv2.line(img, (x2, y), (x2, min(y + dash_len, y2)), color, thickness)


class FPSCounter:
    def __init__(self, avg=30):
        self.t0 = time.time()
        self.frames = 0
        self.avg = avg
        self._hist = []

    def tick(self):
        now = time.time()
        dt = now - self.t0
        self.t0 = now
        self.frames += 1
        if dt > 0:
            self._hist.append(1.0 / dt)
            if len(self._hist) > self.avg:
                self._hist.pop(0)
        return self.fps

    @property
    def fps(self):
        return sum(self._hist) / len(self._hist) if self._hist else 0.0
