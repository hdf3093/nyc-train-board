#!/bin/bash
# One-shot Pi setup for the train board. Run on the Pi from ~/train-board:
#     bash setup.sh
# Takes ~15-20 minutes on a Pi 3B+ (most of it is building the LED library).
# Safe to re-run if something fails partway.
set -e
cd "$(dirname "$0")"
DIR="$PWD"
PILLOW=13.0.0   # must match requirements.txt

echo "== 1/6  System packages"
sudo apt-get update -q
sudo apt-get install -y -q git python3-venv python3-dev cython3 curl

echo "== 2/6  Turn off onboard audio (it fights the LED timing)"
echo "blacklist snd_bcm2835" | sudo tee /etc/modprobe.d/blacklist-rgb-matrix.conf >/dev/null

echo "== 3/6  Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== 4/6  LED library (the slow part, ~10-15 min)"
[ -d rpi-rgb-led-matrix/.git ] || { rm -rf rpi-rgb-led-matrix; git clone -q https://github.com/hzeller/rpi-rgb-led-matrix.git; }
( cd rpi-rgb-led-matrix/bindings/python/rgbmatrix/shims
  for f in Imaging.h ImPlatform.h Mode.h Arrow.h ImagingUtils.h; do
    curl -fsSLO "https://raw.githubusercontent.com/python-pillow/Pillow/$PILLOW/src/libImaging/$f"
  done )
( cd rpi-rgb-led-matrix && "$DIR/.venv/bin/pip" install -q . )

echo "== 5/6  Start the board automatically on power-up"
sudo cp train-board.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable train-board

echo "== 6/6  Reliability: Wi-Fi power saving off, freeze watchdog, own CPU core for the LEDs"
printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/wifi-powersave-off.conf >/dev/null
sudo sed -i 's/^#\?RuntimeWatchdogSec=.*/RuntimeWatchdogSec=15/' /etc/systemd/system.conf
grep -q isolcpus=3 /boot/firmware/cmdline.txt || sudo sed -i 's/$/ isolcpus=3/' /boot/firmware/cmdline.txt

echo
echo "All set. Rebooting now; the board will light up by itself in about a minute."
sleep 3
sudo reboot
