"""
检测模块：多模型融合 + CV 兜底
  - 鸟类：Ultralytics YOLOv8n (COCO)，取 bird 类
  - 电线/电线杆：
      * 若配置了 weights 且文件存在 -> 深度学习（如 Thalos Powerline 模型）
      * 否则 -> OpenCV 直线检测兜底（cv_fallback）
所有结果统一映射为中文标签+颜色输出。
支持 CUDA / CPU 自动回退。
"""
import os
from ultralytics import YOLO
import cv_fallback


class MultiModelDetector:
    def __init__(self, models_cfg: dict):
        self.device = models_cfg.get("device", "cuda")
        self.imgsz = int(models_cfg.get("imgsz", 640))
        self.tracking = models_cfg.get("tracking", True)  # ByteTrack 追踪开关
        self.auto_cascade = models_cfg.get("auto_cascade", False)  # 自动挡级联
        self.conf_override = None  # 实时灵敏度（置信度阈值）覆盖，None=用 spec 默认
        if self.device == "cuda" and not _cuda_available():
            print("[detector] 未检测到 CUDA，回退到 CPU。")
            self.device = "cpu"

        self.dl_specs = []   # (name, model, spec)  深度学习
        self.cv_specs = []   # (name, cv_cfg)        CV 兜底
        for name, spec in models_cfg.items():
            if name in ("device", "imgsz"):
                continue
            if not isinstance(spec, dict) or not spec.get("enabled", True):
                continue
            method = spec.get("method", "auto")  # auto | dl | cv
            weights = spec.get("weights")
            use_dl = False
            if method in ("auto", "dl"):
                if weights and os.path.exists(weights):
                    use_dl = True
                elif method == "dl":
                    print(f"[detector] 模型 {name} 要求深度学习但权重缺失: {weights}")
                    continue
            if use_dl:
                try:
                    model = YOLO(weights)
                    model.to(self.device)
                    self.dl_specs.append((name, model, spec))
                    print(f"[detector] 已加载深度学习模型 '{name}' -> {weights} (device={self.device})")
                except Exception as e:
                    print(f"[detector] 加载模型 {name} 失败: {e}，改用 CV 兜底。")
                    if spec.get("cv"):
                        self.cv_specs.append((name, spec["cv"]))
            else:
                cv_cfg = spec.get("cv")
                if cv_cfg:
                    self.cv_specs.append((name, cv_cfg))
                    print(f"[detector] 模型 '{name}' 使用 OpenCV 直线检测兜底（无需下载权重）。")
                else:
                    print(f"[detector] 模型 {name} 既无权重也无 CV 配置，跳过。")

        # 按 cascade_level 升序一次性排序（自动挡级联依赖此顺序）。
        # 不放在 detect() 里，避免每帧重复排序浪费 CPU。
        self.dl_specs.sort(key=lambda x: int(x[2].get("cascade_level", 9)))

    def detect(self, frame):
        """
        输入 BGR 帧，返回检测列表：
        [{'bbox':(x1,y1,x2,y2), 'label':str, 'color':(b,g,r), 'conf':float, 'source':str}, ...]
        """
        out = []
        # 深度学习（自动挡级联：dl_specs 已在 __init__ 按 cascade_level 升序排好）
        best_cascade_seen = 0
        # 深度学习
        for name, model, spec in self.dl_specs:
            dev = self.device
            conf = float(spec.get("conf", 0.3))
            if self.conf_override is not None:
                conf = self.conf_override
            iou = float(spec.get("iou", 0.45))
            try:
                if self.tracking:
                    res = model.track(
                        frame,
                        conf=conf,
                        iou=iou,
                        imgsz=self.imgsz,
                        device=dev,
                        persist=True,
                        verbose=False,
                    )[0]
                else:
                    res = model.predict(
                        frame,
                        conf=conf,
                        iou=iou,
                        imgsz=self.imgsz,
                        device=dev,
                        verbose=False,
                    )[0]
            except Exception as e:
                # CUDA 首次失败后永久切换到 CPU，不再每帧重试（避免 GPU 无用抖动）
                if dev != "cpu":
                    print(f"[detector] ⚠ 模型 {name} CUDA 推理失败: {e}")
                    print(f"[detector] 永久切换到 CPU 模式（后续不再尝试 CUDA）。")
                    self.device = "cpu"
                    try:
                        res = model.predict(
                            frame,
                            conf=conf,
                            iou=iou,
                            imgsz=self.imgsz,
                            device="cpu",
                            verbose=False,
                        )[0]
                    except Exception as e2:
                        print(f"[detector] 模型 {name} CPU 推理也失败: {e2}")
                        continue
                else:
                    print(f"[detector] 模型 {name} 推理异常: {e}")
                    continue
            class_map = spec.get("classes", {})
            names = getattr(model, "names", {})
            for box in res.boxes:
                cls_id = int(box.cls[0])
                name_key = names.get(cls_id, str(cls_id))
                meta = class_map.get(name_key)
                if not meta or not meta.get("enabled", True):
                    continue
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                track_id = int(box.id[0]) if box.id is not None else None
                out.append({
                    "bbox": (x1, y1, x2, y2),
                    "label": meta.get("label", name_key),
                    "color": tuple(int(c) for c in meta.get("color", (0, 255, 0))),
                    "conf": float(box.conf[0]),
                    "source": name,
                    "track_id": track_id,
                })
            # 自动挡级联：如果当前模型找到足够高置信度目标，跳过更重的后续模型
            if self.auto_cascade:
                level = int(spec.get("cascade_level", 9))
                best_cascade_seen = max(best_cascade_seen, level)
                high_conf = sum(1 for d in out if d.get("conf", 0) >= 0.35 and d.get("source") == name)
                if high_conf >= 2:
                    # 跳过所有 cascade_level 更高的模型
                    break
        # CV 兜底（电线/电线杆）
        for name, cv_cfg in self.cv_specs:
            try:
                out.extend(cv_fallback.detect_lines(frame, cv_cfg))
            except Exception as e:
                print(f"[detector] CV 兜底 {name} 异常: {e}")
        return out


    def active_models(self):
        """返回当前真正在跑的模型描述（用于 UI 顶部显示「实际调用模型」）。

        注意：返回的是程序运行时实际加载/调用的模型（含 CV 直线兜底），
        而非 config 里的配置段名（如 'powerline'）。
        """
        out = []
        for name, model, spec in self.dl_specs:
            w = spec.get("weights", "?")
            out.append(f"{name}→{os.path.basename(str(w))}")
        for name, cv_cfg in self.cv_specs:
            out.append(f"{name}→CV直线兜底")
        if not out:
            return ["识别未加载/已关闭"]
        return out

def _cuda_available():
    try:
        import torch
        # is_available 在某些环境会误报 True（实际 device_count=0），
        # 必须同时确认确实有可用 GPU，否则后续 predict 会报错。
        return bool(torch.cuda.is_available() and torch.cuda.device_count() > 0)
    except Exception:
        return False


def list_model_classes(weights):
    """辅助：打印某权重文件的全部类别名（调试用）。"""
    m = YOLO(weights)
    print(f"{weights} 类别: {m.names}")
