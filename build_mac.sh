#!/bin/bash
cd "$(dirname "$0")"
echo "Building HiFi Master for macOS..."
pip3 install sounddevice soundfile numpy pyinstaller
pyinstaller --onefile --name "HiFiMaster" --windowed --clean \
  --hidden-import=sounddevice \
  --hidden-import=soundfile \
  --hidden-import=numpy \
  src/hifi_player.py
echo ""
echo "Done! Check dist/HiFiMaster"
