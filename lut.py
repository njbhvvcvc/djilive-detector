"""LUT 工具：导入 .cube 3D LUT 并应用，浓度 0-200（默认 100）。

- load_cube(path)        : 解析 .cube 3D LUT，返回 (lut3d, size, title)
- make_warm(size)        : 生成一个内置「暖阳」调色 LUT
- apply_lut(frame, ...)   : 把 3D LUT 应用到 BGR 帧，按浓度混合

浓度语义：0=原图；100=完整 LUT 效果；200=2 倍强度（过曝式风格化）。
使用 scipy.ndimage.map_coordinates 做 C 级三线性插值，720p 单帧约几毫秒。
"""
import numpy as np
from scipy.ndimage import map_coordinates


def load_cube(path):
    """解析 .cube 3D LUT 文件。

    返回 (lut3d, size, title)：
      lut3d : np.ndarray, shape=(size, size, size, 3), dtype=float32, 取值 [0,1]
               索引顺序 [b, g, r]（与 .cube 规范一致：R 变化最快）。
      size  : LUT_3D_SIZE
      title : TITTLE 字段（若无则为 None）
    """
    size = None
    title = None
    data = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            up = s.upper()
            if up.startswith("TITLE"):
                try:
                    title = s.split(None, 1)[1].strip().strip('"')
                except Exception:
                    pass
                continue
            if up.startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
                continue
            # 1D LUT 或 DOMAIN 等不支持字段，跳过
            if up.startswith("LUT_1D_SIZE") or up.startswith("DOMAIN_"):
                continue
            parts = s.split()
            if len(parts) >= 3:
                try:
                    data.append((float(parts[0]), float(parts[1]), float(parts[2])))
                except ValueError:
                    continue
    if size is None:
        raise ValueError("不是有效的 .cube：缺少 LUT_3D_SIZE")
    if len(data) < size ** 3:
        raise ValueError(f"LUT 数据不足：需要 {size ** 3} 行，实际 {len(data)} 行")
    arr = np.array(data[: size ** 3], dtype=np.float32)
    if arr.max() > 1.5:          # 部分 .cube 用 0-255，部分用 0-1
        arr = arr / 255.0
    arr = np.clip(arr, 0, 1)
    lut3d = arr.reshape((size, size, size, 3))
    return lut3d, size, title


def make_identity(size=17):
    """生成恒等 LUT（输出=输入）。"""
    idx = np.linspace(0, 1, size, dtype=np.float32)
    b, g, r = np.meshgrid(idx, idx, idx, indexing="ij")
    lut3d = np.empty((size, size, size, 3), dtype=np.float32)
    lut3d[:, :, :, 0] = b
    lut3d[:, :, :, 1] = g
    lut3d[:, :, :, 2] = r
    return lut3d, size


def make_warm(size=17):
    """内置「暖阳」LUT：提升红、压低蓝，高光更暖。"""
    lut3d, size = make_identity(size)
    out = lut3d.copy()
    b = out[:, :, :, 0].copy()
    g = out[:, :, :, 1].copy()
    r = out[:, :, :, 2].copy()
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    r = np.clip(r * 1.10 + 0.04 * lum, 0, 1)
    g = np.clip(g * 1.02 + 0.01 * lum, 0, 1)
    b = np.clip(b * 0.90 - 0.02 * lum, 0, 1)
    out[:, :, :, 0] = b
    out[:, :, :, 1] = g
    out[:, :, :, 2] = r
    return out, size


def apply_lut(frame_bgr, lut3d, size, concentration=100):
    """把 3D LUT 应用到 BGR 帧。

    参数：
      frame_bgr    : uint8 ndarray (h, w, 3)，OpenCV 默认 BGR
      lut3d, size  : load_cube / make_* 的返回值
      concentration : 0-200，默认 100
    返回：应用后的 uint8 BGR 帧（concentration<=0 时原样返回）
    """
    if lut3d is None or concentration <= 0 or size < 2:
        return frame_bgr
    conc = float(np.clip(concentration / 100.0, 0.0, 2.0))
    img = frame_bgr.astype(np.float32) / 255.0      # b, g, r in [0,1]
    max_idx = size - 1
    # 坐标数组 (3, N)：axis0=b, axis1=g, axis2=r
    coords = np.stack([
        np.clip(img[:, :, 0] * max_idx, 0, max_idx).ravel(),
        np.clip(img[:, :, 1] * max_idx, 0, max_idx).ravel(),
        np.clip(img[:, :, 2] * max_idx, 0, max_idx).ravel(),
    ], axis=0)
    h, w = img.shape[:2]
    graded = np.empty((h, w, 3), dtype=np.float32)
    for c in range(3):
        vol = lut3d[:, :, :, c]
        mapped = map_coordinates(vol, coords, order=1, mode="nearest")
        graded[:, :, c] = mapped.reshape(h, w)
    out = img + (graded - img) * conc
    return np.clip(out * 255.0, 0, 255).astype(np.uint8)
