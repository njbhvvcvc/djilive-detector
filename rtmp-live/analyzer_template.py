# -*- coding: utf-8 -*-
"""
AI 分析模块模板 —— DJI 本地直播启动器的「识别口子」
================================================

用法:
  1. 把本文件复制/重命名为  analyzer.py  (必须同名!), 放到 DJILive.exe 同一个文件夹里。
  2. 实现下面的 analyze(frame) 函数:
       入参 frame : numpy.ndarray, shape=(H,W,3), 颜色空间 BGR (OpenCV 默认)
       返回       : 同形状的 numpy.ndarray (BGR)。保持不变即原样显示。
  3. 重新点『③ 开始预览』即可生效, 无需重新打包 exe。

提示:
  - 不要在主线程做太重的同步推理, 否则预览掉帧。需要重模型时建议降采样/降频。
  - 定义 ANALYZER_NAME = "xxx" 会在启动器里显示当前 AI 模块名。
  - 接入 YOLO 等: 在文件顶部 import 你的模型 (自行 pip 安装到本机 Python,
    或改为调用本地推理 HTTP 服务), 在 analyze 里调用即可。

下面示例默认只在左上角叠加时间戳, 演示「如何往画面上写东西」。
把它换成你自己的检测/识别逻辑就行。
"""

ANALYZER_NAME = "示例模板 (时间戳叠加)"

# ---- 想接 YOLO 时, 取消下面注释并 pip install ultralytics ----
# from ultralytics import YOLO
# model = YOLO("yolov8n.pt")   # 首次会自动下载


def analyze(frame):
    """示例: 左上角叠加当前时间, 演示信息叠加。
    替换成你的识别逻辑 (如目标检测画框、分割蒙版、文本 OCR 等)。
    """
    import cv2
    import datetime
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    cv2.putText(frame, ts, (12, 32), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (0, 255, 0), 2, cv2.LINE_AA)

    # ---- YOLO 示例 (需先取消顶部 import 并装好 ultralytics) ----
    # results = model(frame, verbose=False)
    # for r in results:
    #     for b in r.boxes:
    #         x1, y1, x2, y2 = map(int, b.xyxy[0])
    #         cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
    #         cv2.putText(frame, r.names[int(b.cls[0])], (x1, y1 - 6),
    #                     cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

    return frame
