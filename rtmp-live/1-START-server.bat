@echo off
cd /d "%~dp0"
title DJI Air 3S - Local Live RTMP Server (keep open)
echo(
echo ==========================================================
echo    DJI Air 3S  -  LOCAL LIVE  (RTMP receiver, no upload)
echo ==========================================================
echo(
echo [1] Your PC LAN IPv4 address(es):
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo        %%a
echo(
echo [2] On the DJI RC2 -> DJI Fly -> Live streaming -> RTMP / Custom,
echo     enter this address (use the 192.168.x.x IP shown above):
echo(
echo        rtmp://YOUR_PC_IP:1935/live/drone1
echo(
echo     ( Reminder: RC2 live needs a USB-C microphone plugged in,
echo       and the RC2 must be on the SAME Wi-Fi/LAN as this PC. )
echo(
echo [3] After the drone starts streaming, watch it by double-clicking:
echo        2-WATCH.bat
echo     (or open in browser)  http://localhost:8889/live/drone1
echo(
echo ==========================================================
echo    Server is starting... KEEP THIS WINDOW OPEN while flying.
echo    Press Ctrl+C or close window to stop.
echo ==========================================================
echo(
mediamtx.exe
echo(
echo Server stopped.
pause
