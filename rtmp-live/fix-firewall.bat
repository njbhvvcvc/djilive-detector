@echo off
cd /d "%~dp0"

:: 若不是管理员，自动请求提权
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 需要管理员权限，正在请求提权...
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

echo ============================================
echo    DJI 直播 - 一键防火墙放行
echo ============================================
echo.
echo 正在为 RC2 推流 / 本机观看放行端口...

:: 先删旧的同名规则，避免重复
netsh advfirewall firewall delete rule name="DJI Live RTMP"     >nul 2>&1
netsh advfirewall firewall delete rule name="DJI Live RTSP"     >nul 2>&1
netsh advfirewall firewall delete rule name="DJI Live HLS"      >nul 2>&1
netsh advfirewall firewall delete rule name="DJI Live WebRTC"   >nul 2>&1
netsh advfirewall firewall delete rule name="DJI Live WebRTCUDP">nul 2>&1

netsh advfirewall firewall add rule name="DJI Live RTMP"      dir=in action=allow protocol=TCP localport=1935
netsh advfirewall firewall add rule name="DJI Live RTSP"      dir=in action=allow protocol=TCP localport=8554
netsh advfirewall firewall add rule name="DJI Live HLS"       dir=in action=allow protocol=TCP localport=8888
netsh advfirewall firewall add rule name="DJI Live WebRTC"    dir=in action=allow protocol=TCP localport=8889
netsh advfirewall firewall add rule name="DJI Live WebRTCUDP" dir=in action=allow protocol=UDP localport=8189

echo.
echo 完成 ✅ 现在让 RC2 连电脑的局域网 IP 即可推流。
echo （注意：RC2 和电脑必须在同一 Wi-Fi / 电脑热点）
echo.
pause
