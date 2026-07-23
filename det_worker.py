"""独立检测进程。

在自有 GIL 中跑 YOLO 推理 + 报警/提醒，结果经 multiprocessing.Queue 回传，
彻底不占用主进程（tkinter/读帧/渲染）的 GIL，因此 AI 推理再慢也绝不会让播放卡顿。

健壮性设计（解决「GPU 识别跑一会儿就停」）：
  - 任何单帧推理异常（含 CUDA OOM）都被细粒度 try 吞掉并释放显存、跳过本帧，
    绝不退出循环 → 识别可持续运行。
  - 结果队列满（主线程 UI 卡顿来不及取）时「丢弃式」入队（safe_put），
    绝不抛 QueueFull 让外层崩溃 → 杜绝「队列满 → 子进程被未捕获异常打死」的死法。
  - 整个 per-message 循环再包一层兜底 try，任何遗漏异常都 continue，保证 worker 永不退出。

注意：本模块顶部只 import 标准库，所有重依赖（torch/ultralytics 等）都放在
detector_process() 函数内部延迟导入——这样 Windows spawn 子进程时不会一上来就加载 GUI。
"""
import time
import traceback
import queue as _queue


def detector_process(in_q, out_q, cfg):
    # ---- 延迟导入（仅在子进程内执行）----
    from frame_analysis import analyze_frame, apply_quality
    from detector import MultiModelDetector
    from trajectory import TrajectoryTracker
    from utils import FPSCounter

    det = None
    tracker = TrajectoryTracker(predict_ttl=0.8)
    states = {"fps": FPSCounter(), "alarm": {}, "alerts": {}, "sighting": {}}

    def safe_put(item):
        # 丢弃式入队：队列满就丢（UI 落后时本帧不叠加，下一帧再说），绝不抛异常
        try:
            out_q.put_nowait(item)
        except _queue.Full:
            pass

    def _free_gpu():
        try:
            import torch as _torch
            if _torch.cuda.is_available():
                _torch.cuda.empty_cache()
        except Exception:
            pass

    def build(models_cfg, quality, tracking, cascade):
        nonlocal det
        try:
            spec = apply_quality(models_cfg, quality)
            det = MultiModelDetector(spec)
            det.tracking = tracking
            det.auto_cascade = cascade
            safe_put(
                ("MODEL", " · ".join(det.active_models()) +
                 f" [{'CUDA' if det.device == 'cuda' else 'CPU'}]"))
        except Exception as e:
            det = None
            safe_put(("ERR", f"模型加载失败: {e}"))

    _fc = 0  # 帧计数：用于节流式释放 CUDA 缓存（每 60 帧一次）
    # 主循环：任何异常都必须被吞掉，保证 worker 永不退出（识别可持续）。
    while True:
        try:
            msg = in_q.get()
        except Exception:
            # 队列被异常关闭：安全退出
            break
        try:
            if msg is None:
                break
            if not isinstance(msg, tuple):
                continue
            tag = msg[0]
            if tag == "BUILD":
                build(msg[1], msg[2], msg[3], msg[4])
            elif tag == "CFG":
                if det is not None:
                    det.tracking = msg[1]
                    det.auto_cascade = msg[2]
            elif tag == "CONF":
                if det is not None:
                    det.conf_override = msg[1]
            elif tag == "FRAME":
                if det is None:
                    continue
                f, t = msg[1], msg[2]
                states["fps"].tick()  # 每收到一帧即累加，驱动画面 FPS 显示
                try:
                    d, ar, at, si, info = analyze_frame(f, det, cfg, states)
                    tracker.update(d, t)
                    interp = tracker.interpolate(t)
                    all_d = d + [di for di in interp if di.get("_predicted")]
                    safe_put((all_d, ar, at, si, info))
                except Exception as e:
                    # 推理异常（含 CUDA OOM）：打印堆栈 + 释放显存 + 跳过本帧，
                    # 不退出循环 → 下一帧仍可正常识别（自愈）。
                    traceback.print_exc()
                    _free_gpu()
                    safe_put(("ERR", f"推理异常(已跳过本帧): {e}"))
            # 不再每帧清空 CUDA 缓存：每帧 empty_cache() 会强制设备同步、
            # 打断推理与前后帧的 GPU 重叠，显著拖慢吞吐。改为每 60 帧释放一次
            # 作为兜底（仍自愈），推理 OOM/异常时仍见上方 except 按需释放。
            _fc += 1
            if _fc >= 60:
                _fc = 0
                _free_gpu()
        except Exception:
            # 兜底：任何遗漏异常都吞掉并继续，worker 绝不退出
            try:
                traceback.print_exc()
            except Exception:
                pass
            _free_gpu()
            continue
