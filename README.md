# DJI 实时视觉识别系统

实时识别 DJI 无人机画面中的 **鸟类 / 电线 / 电线杆**，并在画面上实时框出。
针对 **RTX 3050 Ti (4GB)** 本地运行优化，全部推理在显卡完成、画面不外传。

## 当前版本状态（2026-07-21）
- ✅ 鸟类：Ultralytics **YOLOv8n (COCO)** 深度学习，已在你的 3050 Ti 上验证（`device=cuda` 生效）。
- ✅ 电线 / 电线杆：**OpenCV 直线检测兜底**（零下载、实时极快）**现在就能用**。
  原理：电线≈近水平细直线，电线杆≈近垂直结构，用 Canny + Hough 直线检测按角度分桶框出。
- ✅ **危险靠近预警（task ②）已实装并验证**：鸟 / 无人机 / 人 等靠近 电线 / 电线杆 / 近电类别时，
  画面自动弹红色脉冲横幅 + 连线标距离 + （可选）系统蜂鸣。CV 兜底与深度学习两种模式都生效。
- 🔧 **深度学习电线模型（task ①）——已「救活」可加载，待补全自定义层即启用**：
  - **已做到**：把本项目 venv 的 `ultralytics` 换成了训练该权重所用的 **A-YOLOM 分支（8.0.105）**，
    并修好了该分支在本机（Python 3.13 + torch 2.6）的两个加载拦路虎：
    ① `pkg_resources` 缺失（用 `packaging` 做了兼容 shim）；
    ② torch≥2.6 默认 `weights_only=True` 拒绝自定义类（已显式设 `False`）。
    现在 `PowerLine-MTYOLO-NANO-150Epochs.pt` **已能被 torch 正常加载**（不再报 `MultiModel` 找不到）。
  - **还差一步（关键澄清）**：该权重推理还需 **`ultralytics.nn.extra_modules`**（论文提出的 SDPM/HAD/EFR 四个自定义模块）。
    但注意——**这段代码根本不在 GitHub 上**：`phd-benel/PowerLine-MTYOLO` 只是论文仓，只放了 `.pt` 权重 + README；
    真正的训练/推理 `ultralytics` 分支在作者 **Google Drive** 的「Code Availability」链接里（github.com 主站虽能访问，但那份代码不在 github）。
    所以：在你**自己机器**（能开 Google Drive）上下那个 Drive 文件夹，把里面的 `ultralytics/` 整目录覆盖到
    `F:\djilive-detector\.venv\Lib\site-packages\ultralytics\` 即可全自动切到深度学习；
    在此之前系统**默认走 CV 直线兜底（照常工作）**——`powerline.method: auto` 已有，加载失败自动回退。
  - **能直接 drop-in 的仍是「标准 YOLOv8 检测权重」**（如 HuggingFace `thalostech2025/thalos-powerline-safety-v1`、
    Google Drive 的 TIC `tic_nano.pt`）。一键拉取：`.venv/Scripts/python.exe download_powerline_model.py`。
  - ⚠️ 注意：venv 的 ultralytics 现被**钉在 A-YOLOM 8.0.105 分支**（为兼容 PowerLine 权重）。
    鸟类模型（YOLOv8n）在此分支下已验证可正常加载+推理；若日后 `pip install -U ultralytics` 升级，
    会把该分支覆盖掉，届时 PowerLine 权重又会变回「需配套代码」状态（鸟类不受影响，会自动回退）。
- 🚄 **高铁/火车识别 + 屏幕红闪报警（新增）**：**无需新模型**，直接复用鸟类用的 `yolov8n.pt`
  （COCO 第 6 类 `train` 即列车），在 `config.yaml` 的 `alerts:` 段默认开启「识别出高铁/火车 → 全屏红闪 + 自定义横幅 + 蜂鸣」。
  详见下方「自定义报警」一节。
- 📊 实测稳态推理 **~21ms/帧 ≈ 47 FPS**（仅推理，3050 Ti + CUDA；鸟类走 GPU，电线走 CV）。

## 它能做什么
- 抓取 `DJILive.exe` 的实时画面（dxcam 按窗口标题低延迟抓取；失败回退 mss 截屏；也可抓整屏/指定区域）
- 实时绘制彩色边框 + 中文标签 + 置信度 + FPS
- **危险靠近预警**：鸟 / 无人机 / 人 等靠近 电线 / 电线杆 / 近电类别时，自动红色脉冲横幅 + 连线标距离告警（详见下节）
- 本地窗口实时显示；亦可一键推流到浏览器（手机/云端远程查看）

## 危险靠近预警（task ②）
无人机靠近带电体非常危险。系统实时判断两类情况并告警：

1. **距离型**：「危险物」(鸟 / 无人机 / 人，见 `alarm.hazard_labels`) 的检测框，
   与「带电体」(电线 / 电线杆 / 设备近电 / 车辆近电 / 吊臂近电 / 高风险靠近) 的检测框
   **边缘距离** 小于阈值即告警：
   - `warn_px`(默认 120px)：较近 → 黄色“注意：目标靠近带电体”
   - `danger_px`(默认 50px)：非常近 → 红色脉冲“危险靠近预警！”+ 两框连线标距离
2. **直接型**：若接入了深度学习电线模型（如 Thalos），它直接输出
   `high_risk_proximity` / `设备近电` / `车辆近电` / `吊臂近电` 等类别 → 直接判最高告警（无需距离判断）。

> 无论当前是 CV 直线兜底 还是 深度学习模式，告警都生效；本地窗口与网页推流都显示。

调参在 `config.yaml` 的 `alarm:` 段：
```yaml
alarm:
  enabled: true
  hazard_labels: ["鸟", "无人机", "人"]          # 危险物
  target_labels: ["电线", "电线杆", "设备近电", "车辆近电", "吊臂近电", "高风险靠近"]
  warn_px: 120      # 黄：较近
  danger_px: 50      # 红：非常近
  direct_danger_labels: ["高风险靠近", "设备近电", "车辆近电", "吊臂近电"]
  show_links: true    # 画 危险物↔带电体 连线与距离
  beep: false         # 危险时 Windows 系统蜂鸣（改 true 开启）
  beep_cooldown: 1.5
```
自测：` .venv/Scripts/python.exe verify_alarm.py `

## 看见提醒：高铁 / 鸟（新增 `sighting`）
高铁、鸟**不是「威胁」**，看见了就好——所以它们**不走红闪威胁报警**，
而是走一套更轻的「看见提醒」：

- **看见提醒**：画面顶部弹出一条**青色软横幅**「看见提醒：高铁/火车、鸟」（不红闪、不整屏红框，不吓人）。
- **自动拍照（默认关闭）**：默认**不拍照**。若想要「看见就拍一张取证」，把 `config.yaml` 里
  `sighting.capture` 改为 `true` 即可——某目标**第一次出现**的瞬间自动存一张到 `captures/sighting/`（带标签+时间戳），不会每帧狂存。

- 高铁/火车**无需新模型**：复用鸟类 `yolov8n.pt`（COCO 第 6 类 `train` = 列车），
  `config.yaml` 的 `models.bird.classes` 已加 `train -> 高铁/火车`。
- 完全**配置驱动**（`config.yaml` 的 `sighting:` 段）：`targets` 列表可任意增删（默认 `高铁/火车`、`鸟`）；
  `capture: true/false` 开关自动拍照（**默认 false**，仅提醒不拍照）；`banner_color` 改提醒色；`capture_dir` 改保存目录。

```yaml
sighting:
  enabled: true
  remind: true             # 顶部青色「看见提醒」软横幅
  capture: false           # 默认不自动拍照（仅提醒）；改为 true 才会在新出现时存一张图
  capture_dir: "captures/sighting"
  banner_color: [0, 255, 255]   # 青色(BGR)，与危险红区分
  targets:
    - label: "高铁/火车"
    - label: "鸟"
```

> 红闪「威胁报警」（`alerts:` 段，全屏红闪）**保留给真正的危险**——例如鸟/无人机/人靠近电线杆的
> 「危险靠近」已由 `alarm.py` 用红色脉冲横幅 + 连线标距离处理。高铁/鸟不属于这类，故默认已移出红闪。
自测：` .venv/Scripts/python.exe verify_sighting.py `（12/12 用例通过）

## 目录结构
```
djilive-detector/
├── config.yaml        # 所有参数（取流方式、置信度、标签颜色、CV兜底参数等）
├── capture.py         # 画面采集（dxcam / mss）
├── detector.py        # 多模型融合 + CV 兜底调度
├── cv_fallback.py     # 电线/电线杆 OpenCV 直线检测兜底
├── utils.py           # 画框 / FPS
├── alarm.py           # 危险靠近预警（距离型 + 直接型）
├── alerts.py          # 自定义报警（识别出即触发：高铁红闪等）
├── main.py            # 本地窗口实时识别（主程序）
├── webstream.py       # 浏览器推流（无界面 / 云端）
├── download_powerline_model.py # 一键拉取电线/杆深度学习权重（多源，CUDA 安全）
├── verify_alarm.py   # 告警逻辑自测
├── verify_alerts.py  # 自定义报警自测（高铁红闪）
├── yolov8n.pt        # 鸟类模型（已下载到本地）
├── models/            # 深度学习电线模型存放处（放了就自动启用）
├── captures/          # 截图保存处
├── wheels/           # torch/torchvision CUDA 版 wheel 备份（重装免下载）
└── .venv/            # Python 虚拟环境（依赖已装好）
```

## 快速开始（Windows + 3050 Ti）
1. 打开 `DJILive.exe`，让无人机画面正常显示。
2. 双击 **`run.bat`**（依赖已装好，首次会自动加载鸟类模型）。
   或命令行：
   ```bat
   .venv\Scripts\python.exe main.py
   ```
3. 程序找到 DJI 窗口并弹出实时识别窗口。
   - `q` 退出  ·  `s` 截图  ·  `p` 暂停

## 远程 / 云端查看（可选）
改 `config.yaml` 里 `web.enabled: true`，然后运行：
```bat
.venv\Scripts\python.exe webstream.py
```
浏览器打开 `http://<本机IP>:8080/`。同一局域网手机即可看；云主机放行 8080 端口可外网访问。

## 单图测试（无需 DJI / 显示器）
```bat
.venv\Scripts\python.exe main.py --test 你的图片.jpg
```
输出检测到的目标并保存 `test_output.jpg`，用于验证管线与显卡。

## 调优建议（3050 Ti）
- **电线太细识别不到**：调高 `config.yaml` 的 `models.imgsz` 到 `800`/`960`（更准但更慢）；CV 兜底可调 `powerline.cv.canny_*` / `threshold` / `min_len`。
- **误报太多**：调高 `conf`（鸟 `0.35`→`0.5`）。
- **想要更高帧率**：降低 `capture.capture_width/height` 或 `imgsz`。
- **换更好的鸟类模型**：把 `models.bird.weights` 指向你自己的 `.pt`，标签按 `classes` 里的“类别名”改。
- **升级为深度学习电线模型（更准，task ①）**：系统已 `method: auto` 就绪。一键拉取：
  `.venv/Scripts/python.exe download_powerline_model.py`（优先 HuggingFace Thalos，次选 Google Drive TIC）。
  下到 `models/powerline_safety_weights.pt` 后**自动切换**为深度学习，无需改代码。
  本机当前网络连不上这两个源，故走 CV 兜底；能访问时重跑该命令即可。
  你也可手动把任意 YOLOv8 电线/杆 `.pt` 改名放入 `models/`；用
  `download_powerline_model.py --check 你的.pt` 可校验它是否为合法 YOLO 权重。

## 模型与许可证
- 鸟类：`yolov8n.pt`（Ultralytics，COCO，AGPL/开源），已在本机。
- 电线/电线杆（标准 drop-in 深度学习版）：`thalostech2025/thalos-powerline-safety-v1`（HuggingFace，AGPL-3.0）。
  类别 `powerline/pole/equipment_near_powerline/vehicle_near_powerline/boom_near_powerline/high_risk_proximity`
  与本项目 `config.yaml` **完全对齐**，且其 `high_risk_proximity` 等即 task ② 的直接型告警来源。
  个人/内部使用通常无碍；若用于产品分发请留意 AGPL 条款。本机当前走 CV 兜底，未使用此权重。
- 参考权重（已拉取，部分「救活」）：`models/PowerLine-MTYOLO-NANO-150Epochs.pt`（GitHub `phd-benel/PowerLine-MTYOLO`，
  MDPI Drones，基于 A-YOLOM 多任务架构）。已通过「换 A-YOLOM 分支 ultralytics + 修 pkg_resources/weights_only」
  让该 `.pt` **可被 torch 加载**；但推理还需其训练分支的 `ultralytics.nn.extra_modules` 自定义层（本机暂未取到），
  故当前未接入自动权重路径，系统走 CV 兜底。补齐 extra_modules 后即全自动启用。
- 环境变更：venv 的 `ultralytics` 已钉在 **A-YOLOM 分支 8.0.105**（为兼容上述权重），并补装了 `pandas`/`scipy`。
- 全部推理在本地 3050 Ti 完成，画面不外传。

## 常见问题
- **找不到 DJI 窗口**：检查 `config.yaml` 的 `capture.window_title`（DJILive 窗口标题通常含 "DJI"）；找不到会自动回退抓取主屏。
- **没有独显 / CUDA 报错**：`config.yaml` 的 `models.device` 改为 `"cpu"`（会变慢）。
- **提示缺模块**：重跑 `setup.bat` 安装依赖。
- **电线/杆框得不准（兜底局限）**：CV 兜底对“细线”效果好、对复杂背景下的杆子较粗。要更高精度请接深度学习权重（见上）。
- **高铁/火车报警怎么用**：无需新模型，已在 `config.yaml` 的 `alerts:` 段默认开启「高铁/火车→红闪」。
  要改触发对象/文字/频率，编辑 `alerts.targets` 即可；`label` 必须与检测输出标签一致（高铁为「高铁/火车」）。
- **深度学习电线权重（PowerLine）状态**：权重现已能加载（torch 层）；但要跑推理还需作者 Drive 里的 `ultralytics.nn.extra_modules`
  （SDPM/HAD/EFR 模块），**该代码在作者 Google Drive，不在 GitHub**（github 主站虽通，但那份代码没上 github）。
  你在能开 Drive 的机器上下那个「Code Availability」文件夹，把其中 `ultralytics/` 覆盖进 venv 的 `site-packages/ultralytics/` 即可。
  另外它是**电缆分割+断股检测**多任务模型，类别是 cable/broken_strand 这类，要接进本项目「电线/电线杆」报警还需做输出→框映射，到时告诉我，我来接。
