@echo off
title DJI Air 3S - Local Viewer
echo(
echo Opening local live viewer in your default browser...
echo(
echo   Primary  (WebRTC, lowest delay ~0.3-1s):
echo     http://localhost:8889/live/drone1
echo(
echo   Backup   (HLS, more stable ~2-5s delay):
echo     http://localhost:8888/live/drone1
echo(
echo   VLC / ffplay users can open:
echo     rtsp://localhost:8554/live/drone1
echo     rtmp://localhost:1935/live/drone1
echo(
start "" "http://localhost:8889/live/drone1"
echo If the page is black: make sure 1-START-server.bat is running AND
echo the drone has begun streaming, then refresh the page.
echo(
timeout /t 8 >nul
