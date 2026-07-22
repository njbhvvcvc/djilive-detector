# sign.ps1 — 对发布 exe 做 Authenticode 签名 + 生成 SHA256 校验和
#
# 用法：
#   自签名（开发 / 演示）：        .\sign.ps1
#   使用已安装的 CA 证书：      .\sign.ps1 -Thumbprint <证书指纹>
#   使用 PFX 文件：            .\sign.ps1 -PfxPath .\cert.pfx
#                                    （会交互式询问 PFX 密码）
#
# 说明：
#   - 自签名证书签出的 exe，Windows 会提示“发布者未知”
#     （Get-AuthenticodeSignature 状态 = UnknownError），属正常。
#   - 用正规 CA 证书（DigiCert / Sectigo 等）签出的 exe 状态 = Valid，
#     用户双击不会出现未知发布者警告。
#   - 始终带时间戳服务器，即使证书未来过期，本次签名依然长期可验。

param(
    [string]$Thumbprint,
    [string]$PfxPath,
    [securestring]$PfxPassword,
    [string]$ExePath = "dist\djilive_detector\djilive_detector.exe",
    [string]$TimestampServer = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

# ---- 1) 解析签名证书 ----
if ($PfxPath) {
    if (-not $PfxPassword) { $PfxPassword = Read-Host -AsSecureString "PFX 密码" }
    $cert = Get-PfxCertificate -FilePath $PfxPath -Password $PfxPassword
}
elseif ($Thumbprint) {
    $cert = Get-ChildItem "Cert:\CurrentUser\My\$Thumbprint" -ErrorAction SilentlyContinue
    if (-not $cert) { $cert = Get-ChildItem "Cert:\LocalMachine\My\$Thumbprint" }
}
else {
    Write-Host "未指定证书 → 创建自签名开发证书（用户打开时会提示‘发布者未知’）" `
        -ForegroundColor Yellow
    $cert = New-SelfSignedCertificate -Type CodeSigningCert `
        -Subject "CN=DJI Live Detector (self-signed dev)" `
        -KeyUsage DigitalSignature -KeySpec Signature `
        -CertStoreLocation "Cert:\CurrentUser\My" -NotAfter (Get-Date).AddYears(3)
}

# ---- 2) 签名（SHA256 + 时间戳）----
Write-Host "正在签名：$ExePath"
Set-AuthenticodeSignature -FilePath $ExePath -Certificate $cert `
    -HashAlgorithm SHA256 -TimestampServer $TimestampServer | Out-Null

# ---- 3) 校验签名状态 ----
$sig = Get-AuthenticodeSignature $ExePath
$color = if ($sig.Status -eq 'Valid') { 'Green' } else { 'Yellow' }
Write-Host ("签名状态：" + $sig.Status + "  (" + $sig.StatusMessage + ")") -ForegroundColor $color

# ---- 4) 生成 SHA256 校验和 ----
$h = Get-FileHash $ExePath -Algorithm SHA256
$line = ($h.Hash.ToLower() + "  " + (Split-Path $ExePath -Leaf))
Set-Content -Path "SHA256SUMS" -Value $line -Encoding ASCII
Write-Host ("SHA256：" + $h.Hash)
Write-Host "已写入 SHA256SUMS"
