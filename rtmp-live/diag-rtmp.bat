@echo off
cd /d "%~dp0"
title DJI 直播 RTMP 自检
echo ============================================
echo      DJI 直播 RTMP 自检
echo ============================================
echo.
echo [1] 本机局域网 IP（RC2 要填这个）:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo      %%a
echo.
echo [2] 端口监听状态（mediamtx 必须在运行才会出现）:
netstat -ano | findstr /E ":1935 :8554 :8888 :8889" || echo      (没有任何监听 -> 先启动 DJILive.exe 或 1-START-server.bat)
echo.
echo [3] 防火墙是否已放行 1935:
netsh advfirewall firewall show rule name="DJI Live RTMP" >nul 2>&1 && (
    echo     已放行 ✅
) || (
    echo     未放行 ❌ -> 右键 fix-firewall.bat 选"以管理员身份运行"
)
echo.
echo [4] 正确排查顺序:
echo     1. 双击 DJILive.exe -> 点"② 启动服务器"
echo     2. RC2: DJI Fly -> 直播 -> 自定义 -> rtmp://上面的IP:1935/live/drone1
echo     3. RC2 必须插 USB-C 麦克风, 且与电脑同一 Wi-Fi / 电脑热点
echo     4. 点 exe 里"③ 开始预览"; 黑屏时看服务器日志有无 "RTMP" 连接记录
echo.
echo [5] 症状速查:
echo     - RC2 提示连接失败 / 超时  -> 99%% 是防火墙或不在同一网段
echo     - RTMP 已连上但画面黑      -> 多半是 H.265 编码, 改用 VLC 打开:
echo       rtsp://localhost:8554/live/drone1  (VLC 能解 H.265)
echo.
echo ============================================
echo  按任意键关闭
pause >nul
