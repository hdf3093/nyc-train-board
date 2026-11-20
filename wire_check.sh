#!/bin/bash
# Wire checker: tells you which Pi signal a jumper wire is carrying.
# Plug the PANEL end of one jumper onto Pi pin 40 (GPIO21, bottom-right corner
# of the header) and this prints which signal that wire is connected to.
# Run with: sudo bash wire_check.sh     (Ctrl+C to stop)

PROBE=21
declare -A NAME=( [11]="R1 (pin 23)" [27]="G1 (pin 13)" [7]="B1 (pin 26)"
                  [8]="R2 (pin 24)"  [9]="G2 (pin 21)"  [10]="B2 (pin 19)"
                  [22]="A (pin 15)"  [23]="B (pin 16)"  [24]="C (pin 18)"
                  [25]="D (pin 22)"  [17]="CLK (pin 11)" [4]="LAT (pin 7)"
                  [18]="OE (pin 12)" )
PINS="4 7 8 9 10 11 17 18 22 23 24 25 27"

cleanup() { for g in $PINS; do pinctrl set $g ip pn; done; pinctrl set $PROBE ip pn; exit; }
trap cleanup INT TERM

for g in $PINS; do pinctrl set $g op dl; done
last=""
echo "Ready. Move one wire's panel end onto Pi pin 40 at a time. Ctrl+C to quit."
while true; do
  found=""
  pinctrl set $PROBE ip pd
  for g in $PINS; do
    pinctrl set $g dh; sleep 0.01
    [ "$(pinctrl lev $PROBE)" = "1" ] && found="$found ${NAME[$g]},"
    pinctrl set $g dl
  done
  if [ -z "$found" ]; then
    pinctrl set $PROBE ip pu; sleep 0.01
    if [ "$(pinctrl lev $PROBE)" = "0" ]; then found=" GND (ground),"; else found=" nothing (no wire, or a bad contact)"; fi
  fi
  msg="Pin 40 is connected to:${found%,}"
  [ "$msg" != "$last" ] && { echo "$msg"; last="$msg"; }
  sleep 0.5
done
