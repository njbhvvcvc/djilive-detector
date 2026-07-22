"""帧分析逻辑（与 tkinter/GUI 完全无关，可被独立进程安全导入）。

原先这些函数写在 app.py 里，会随 app 模块一起加载 tkinter。
为了把 AI 推理搬进独立进程（不再抢占主进程 GIL、从而不拖慢播放），
这里把它们抽到无 GUI 依赖的模块中。
"""
import time

from alarm import analyze as alarm_analyze, maybe_beep as alarm_beep
from sighting import analyze as sighting_analyze, maybe_capture as sighting_capture
from alerts import analyze as alerts_analyze, maybe_beep as alerts_beep
from utils import FPSCounter
from trajectory import TrajectoryTracker


def apply_quality(models_cfg, q):
    """根据精度档位（high/medium/fast）生成新的 models 子配置。"""
    m = dict(models_cfg)
    if q == "high":
        m["imgsz"] = 1280
        m["quality"] = "high"
    elif q == "medium":
        m["imgsz"] = 960
        m["quality"] = "medium"
    else:  # fast
        m["imgsz"] = 960
        m["quality"] = "fast"
    pl = m.get("powerline")
    if isinstance(pl, dict):
        pl = dict(pl)
        if q == "fast":
            pl["weights"] = "models/tic_small.pt"
        else:
            pl["weights"] = "models/powerline_safety_weights.pt"
        pl["method"] = "auto"
        m["powerline"] = pl
    return m


def analyze_frame(frame, det, cfg, states):
    """跑检测+报警+提醒，返回 (dets, alarm_res, atrig, sighted, info_str)。"""
    alarm_cfg = cfg.get("alarm", {})
    sighting_cfg = cfg.get("sighting", {})
    alerts_cfg = cfg.get("alerts", {})

    t0 = time.time()
    dets = det.detect(frame)
    ms = (time.time() - t0) * 1000

    alarm_res = None
    if alarm_cfg.get("enabled", True):
        alarm_res = alarm_analyze(dets, alarm_cfg)
        alarm_beep(alarm_res, alarm_cfg, states["alarm"])

    atrig = []
    if alerts_cfg.get("enabled", False):
        atrig = alerts_analyze(dets, alerts_cfg)
        alerts_beep(atrig, alerts_cfg, states["alerts"])

    sighted = []
    if sighting_cfg.get("enabled", False):
        sighted = sighting_analyze(dets, sighting_cfg)
        for p in sighting_capture(frame, sighted, sighting_cfg, states["sighting"]):
            print(f"[app] 已自动拍照(看见): {p}")

    # 归一化检测框坐标（nbox∈[0,1]），让叠加层与原视频分辨率/帧率解耦：
    # 无论检测在何种缩放帧（feed 降采样到 480 宽）上进行，叠加层都能
    # 按当前显示尺寸正确映射，框不再错位/缩小。
    fh, fw = frame.shape[:2]
    for d in dets:
        bb = d.get("bbox")
        if bb:
            d["nbox"] = (bb[0] / fw, bb[1] / fh, bb[2] / fw, bb[3] / fh)
    info = f"FPS {states['fps'].fps:.0f} | 推理 {ms:.0f}ms | 目标 {len(dets)}"
    if alarm_res is not None:
        if alarm_res.get("danger"):
            info += " | 危险靠近"
        elif alarm_res.get("warn"):
            info += " | 注意靠近"
    if atrig:
        info += " | 自定义报警"
    if sighted:
        info += " | 看见提醒"
    return dets, alarm_res, atrig, sighted, info
