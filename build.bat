@echo off
cd /d "%~dp0"
echo Building HiFi Master...
pyinstaller --onefile --name "HiFiMaster" --noconsole --clean ^
  --hidden-import=sounddevice ^
  --hidden-import=soundfile ^
  --hidden-import=numpy ^
  src\hifi_player.py
echo.
echo Done! Check dist\HiFiMaster.exe
pause
