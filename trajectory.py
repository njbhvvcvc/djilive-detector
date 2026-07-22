"""
轨迹预测模块：在低 FPS 下对检测到的物体做位置插值，让画面看起来更平滑。

用法：
  tracker = TrajectoryTracker(predict_ttl=0.5)
  tracker.update(dets, timestamp)   # 传入真实检测结果
  smoothed = tracker.interpolate(timestamp)  # 获取插值后的完整结果
  
原理：
  - 对每个 label+位置 维护最近 N 帧的位置历史
  - 检测间隔中，用最后 2 个已知位置做线性外推
  - 超过 predict_ttl 秒未更新的目标自动移除
"""
import time


class _Trajectory:
    """单个物体的轨迹。"""

    def __init__(self, label, bbox, color, conf, source, t):
        self.label = label
        self.color = color
        self.source = source
        self.conf = conf
        self.history = [(t, bbox)]
        self.last_seen = t

    def update(self, bbox, conf, t):
        self.history.append((t, bbox))
        self.last_seen = t
        self.conf = conf
        # 只保留最近 4 个位置
        if len(self.history) > 4:
            self.history.pop(0)

    def predict(self, t):
        """根据最后 2 个位置线性外推当前时刻的位置。"""
        if len(self.history) < 2:
            return self.history[-1][1]  # 历史不够就用最后一个已知位置

        (t1, b1), (t2, b2) = self.history[-2], self.history[-1]
        dt = t2 - t1
        if dt <= 0:
            return b2

        # 对每个坐标做线性外推
        x1, y1, x2, y2 = b1
        cx1, cy1, cx2, cy2 = b2
        speed_x1 = (cx1 - x1) / dt
        speed_y1 = (cy1 - y1) / dt
        speed_x2 = (cx2 - x2) / dt
        speed_y2 = (cy2 - y2) / dt

        dt_pred = t - t2
        px1 = int(cx1 + speed_x1 * dt_pred)
        py1 = int(cy1 + speed_y1 * dt_pred)
        px2 = int(cx2 + speed_x2 * dt_pred)
        py2 = int(cy2 + speed_y2 * dt_pred)

        return (px1, py1, px2, py2)


class TrajectoryTracker:
    """轨迹跟踪器。"""

    def __init__(self, predict_ttl=0.5, max_distance=300):
        self.predict_ttl = predict_ttl   # 超过多久没见就干掉预测
        self.max_distance = max_distance  # 前后帧同 label 框距离上限（防跳变关联）
        self._tracks = {}   # key: (label, source, x, y)

    def _key(self, det):
        """用 label+source+框中心附近的位置做唯一键。"""
        x1, y1, x2, y2 = det["bbox"]
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        return (det["label"], det.get("source", ""), cx // 50, cy // 50)

    def _closest(self, det, t):
        """找最近匹配的已有轨迹（按框中心距离）。"""
        cx = (det["bbox"][0] + det["bbox"][2]) // 2
        cy = (det["bbox"][1] + det["bbox"][3]) // 2
        best_dist = self.max_distance
        best_key = None

        label = det["label"]
        source = det.get("source", "")
        for k, tr in self._tracks.items():
            if tr.label != label or tr.source != source:
                continue
            last_box = tr.history[-1][1]
            lcx = (last_box[0] + last_box[2]) // 2
            lcy = (last_box[1] + last_box[3]) // 2
            dist = ((cx - lcx) ** 2 + (cy - lcy) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_key = k

        if best_key is not None:
            return best_key
        return self._key(det)

    def update(self, dets, t=None):
        """用真实检测结果更新轨迹。"""
        if t is None:
            t = time.time()
        used_keys = set()
        for d in dets:
            k = self._closest(d, t)
            used_keys.add(k)
            if k in self._tracks:
                self._tracks[k].update(d["bbox"], d["conf"], t)
            else:
                self._tracks[k] = _Trajectory(
                    d["label"], d["bbox"], d["color"],
                    d["conf"], d.get("source", ""), t)

        # 清理过期的轨迹
        expire = []
        for k, tr in self._tracks.items():
            if t - tr.last_seen > self.predict_ttl:
                expire.append(k)
        for k in expire:
            del self._tracks[k]

    def interpolate(self, t=None):
        """返回当前时刻插值后的所有目标列表。"""
        if t is None:
            t = time.time()
        out = []
        for k, tr in self._tracks.items():
            pred_box = tr.predict(t)
            out.append({
                "bbox": pred_box,
                "label": tr.label,
                "color": tr.color,
                "conf": tr.conf,
                "source": tr.source,
                "_predicted": t - tr.last_seen > 0.05,  # 标记为插值（非实时检测）
            })
        return out
