# 开源验证指南

本项目以 **GPL-3.0** 开源（见 `LICENSE`）。发布包包含：

- `djilive_detector.exe` —— 主程序（已做 Authenticode 代码签名）
- `SHA256SUMS` —— 二进制 SHA256 校验和
- `LICENSE` / `NOTICE` —— 许可证与第三方归属声明

---

## 1. 校验文件完整性（防篡改）

下载后比对 SHA256，与 `SHA256SUMS` 中记录的值一致，说明文件未被
篡改：

```powershell
# 方式 A：PowerShell
(Get-FileHash djilive_detector.exe -Algorithm SHA256).Hash.ToLower()

# 方式 B：certutil
certutil -hashfile djilive_detector.exe SHA256
```

## 2. 校验代码签名（确认真货）

```powershell
Get-AuthenticodeSignature djilive_detector.exe | `
    Select-Object Status, StatusMessage, SignerCertificate
```

- **官方 CA 证书签名**：`Status = Valid`，双击 exe 不会出现“未知发布者”警告。
- **自签名开发版**：`Status = UnknownError`
  （提示“终止于不受信任的根证书”），属正常 —— 仅说明该构建用自签名
  证书，并非官方 CA 签发，不代表文件有问题。

也可：右键 exe → 属性 → “数字签名” 选项卡查看签名详情。

## 3. 源码与依赖

- 完整源码见本仓库。
- 第三方组件与音乐 API 归属见 `NOTICE`。
- 修改并分发时，整体须仍以 GPL-3.0（或兼容许可）开源。

## 4. 重新签名（维护者）

见 `sign.ps1`：

```powershell
# 用已安装的 CA 证书（推荐，状态为 Valid）
.\sign.ps1 -Thumbprint <你的证书指纹>

# 或用 PFX 文件
.\sign.ps1 -PfxPath .\cert.pfx
```

> 自签名证书仅用于本地开发验证管道；对外发布请使用正规代码签名
> 证书（约 $200–400 / 年），用户才不会看到“发布者未知”。
