#!/usr/bin/env python3
"""
Train + weather board for a two-panel 128x32 LED matrix: live NYC subway times,
weather, and a couple of kawaii bears. Put your own station, location and special
dates in my_settings.py (see my_settings.example.py); it's loaded at startup and
never needs to be shared.

Two 64x32 RGB LED panels chained side by side (128x32):
  left  — next two L and M trains you can still catch, colored by urgency
          (a line with no trains shows the MTA's alert for it instead)
  right — animated weather, rain/snow warning, 24h clock, and a little bear
          whose outfit follows the weather (umbrella, scarf, beanie, nightcap)

Data (all free, no API keys):
  MTA GTFS-realtime train feeds + subway alerts, Open-Meteo weather.

Run modes:
  python train_board.py               # drive the LED panels (on the Raspberry Pi)
  python train_board.py --terminal    # live preview in your terminal (any computer)
  python train_board.py --png out.png # render one frame to an image file
"""
import argparse
import random
import functools
import math
import sys
import threading
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests
from google.transit import gtfs_realtime_pb2
from PIL import Image, ImageDraw

# ─── Settings ────────────────────────────────────────────────────────────────
MTA = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"

# Example station: Bedford Av, Manhattan-bound L. Set your own in my_settings.py.
LINES = [
    # stop: GTFS stop id + "N"/"S" direction (look ids up in the MTA's stops.txt)
    # trip_stop(): is a station between you and your destination? (used to filter alerts)
    # origins: starting terminals (trains still sitting there show dimmer)
    dict(route="L", feed=MTA + "nyct%2Fgtfs-l", stop="L08N", walk_min=5,
         origins={"L29N"},                      # Canarsie-Rockaway Pkwy
         trip_stop=lambda s: s.startswith("L") and int(s[1:3]) <= 8),
]
ALERTS_URL = MTA + "camsys%2Fsubway-alerts.json"
HEADER = "MANHATTAN"                  # the title above the train rows (where you're headed)

LAT, LON = 40.7128, -74.0060          # weather location (example: lower Manhattan)


def weather_url():
    return (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,weather_code,is_day"
    "&hourly=precipitation_probability,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min"
    "&temperature_unit=fahrenheit&timezone=America%2FNew_York&forecast_days=2"
    )
RAIN_CHANCE = 50          # % chance that counts as "it's going to rain/snow"
RAIN_LOOKAHEAD_H = 12     # how far ahead to warn about rain/snow
COLD_F = 40               # bear wears a scarf below this temperature
BUDDY_TIMES = ["11:11"]   # Chairoikoguma visits (waving) during these minutes; also whenever it's rainy

# Special days ("MM-DD"): the bear dresses up and a message scrolls once a minute.
# Your own days win over holidays if they ever land on the same date. Put yours in
# my_settings.py, e.g. {"MM-DD": ("birthday", "HAPPY BIRTHDAY!"), "MM-DD": ("anniversary", ...)}
PERSONAL_DAYS = {}
HOLIDAYS = {
    "01-01": ("newyear", "HAPPY NEW YEAR!"),
    "02-14": ("valentine", "HAPPY VALENTINE'S DAY!"),
    "03-17": ("stpatricks", "HAPPY ST. PATRICK'S DAY!"),
    "07-04": ("july4", "HAPPY 4TH OF JULY!"),
    "10-31": ("halloween", "HAPPY HALLOWEEN!"),
    "12-25": ("christmas", "MERRY CHRISTMAS!"),
    # Easter and Thanksgiving are worked out each year
}
# Seasons: spring = sakura petals, summer = sunglasses when it's hot, fall = leaves,
# October = spooky (witch hat, bats), December = Christmas (Santa gear, tree, lights, snow)
HOT_F = 80                # bear wears sunglasses at or above this temperature
WEEKEND_WAKE = 9          # bear sleeps in until 09:00 on Sat/Sun (weekdays: NIGHT_END)
COFFEE_UNTIL = 12         # lazy weekends: hot coffee until noon, then lounging on its belly
YAWN_EVERY_S = 7          # lazy weekend yawns this often
SLIDE_S = 0.4             # how long a changing number takes to slide into place
LETTER_CASE = "lower"     # "lower" (manhattan, 9 min) or "upper" (MANHATTAN, 9 MIN)

TRAINS_EVERY_S, ALERTS_EVERY_S, WEATHER_EVERY_S = 30, 120, 600
TRAINS_STALE_S, WEATHER_STALE_S = 180, 3600
YELLOW_AT_MIN = 2         # leave within this many minutes → yellow; 0 → blinking red
SCROLL_PX_PER_S = 10      # one pixel per frame at 10 fps
FPS = 10                  # animations change 10x a second; drawing faster is wasted CPU
SMOOTH_S = 60             # ignore prediction wobbles smaller than this (stops 6→7→6 flicker)
CLOCK_FIX_AFTER_S = 30    # correct our clock from the MTA's if it's off by more than this

BRIGHTNESS_DAY, BRIGHTNESS_NIGHT = 95, 30   # 0–100
NIGHT_START, NIGHT_END = 23, 7              # hours to dim the panel (and the bear sleeps)
# When the screen is on (the rest of the time it's fully dark; the Pi keeps running)
WEEKDAY_ON, WEEKDAY_OFF = "07:00", "10:00"   # weekdays: just the morning commute
WEEKEND_ON, WEEKEND_OFF = "09:00", "01:00"   # Sat/Sun: from 9am until 1am (past midnight is fine)
# A cute message scrolls across the top every couple of minutes each morning. It's a
# different one every day, going through the whole list before any repeats.
MORNING_UNTIL = 12                           # show it until noon
MESSAGE_EVERY_S = 120                        # she says something every 2 minutes
PAGE_S = 0.9                                 # seconds each line of the speech bubble stays up
MORNING_MESSAGES = [
    # little facts
    "octopuses have three hearts and blue blood",
    "the light hitting you right now left the sun 8 minutes ago",
    "there are more trees on earth than stars in the milky way",
    "a day on venus is longer than its year",
    "sharks are older than trees",
    "cleopatra lived closer to the moon landing than to the pyramids",
    "honey never spoils. archaeologists found 3000 year old jars",
    "bananas are berries. strawberries are not",
    "a group of flamingos is called a flamboyance",
    "sea otters hold hands when they sleep so they don't drift apart",
    "wombats poop cubes",
    "the moon drifts about 4 cm farther from earth every year",
    "koalas have fingerprints almost identical to ours",
    "oxford university is older than the aztec empire",
    "the stars on grand central's ceiling are painted backwards",
    "you share about 60 percent of your dna with a banana",
    "the eiffel tower grows about 15 cm taller in summer",
    "a teaspoon of neutron star weighs about a billion tons",
    "cows have best friends and get stressed when apart",
    "every atom in your body is billions of years old",
    # little questions
    "what's one thing you're looking forward to today?",
    "what would you do today if you knew you couldn't fail?",
    "notice one thing on your walk you've never seen before",
    "who would love to hear from you today?",
    "what's the best thing you've eaten this week?",
    "what's something small that made you happy lately?",
    "if today had a soundtrack, what's track one?",
    "what would 10 year old you think of your life now?",
    "what's a question you'd love to know the answer to?",
    "what's one thing you'd tell yourself a year ago?",
    "if you could add one stop to the L train, where would it go?",
    "what's a skill you'd learn if time didn't exist?",
    # little nudges
    "say yes to one small adventure today",
    "the bears think you should get the good coffee",
    "somewhere a dog is very happy to see its person",
    "today is the oldest you've ever been and the youngest you'll ever be",
    "you've never lived this exact day before",
    "chairo says: be curious about one person today",
    "kiiroitori would like to remind you to look up",
    "8 million people in this city and you're the only you",
]
# Countdowns Korilakkuma mentions every other time she talks (within COUNTDOWN_WITHIN days).
# "MM-DD" repeats every year; "YYYY-MM-DD" is a one-time date.
COUNTDOWNS = [
    ("christmas", "12-25"),
]
COUNTDOWN_WITHIN = 60
MORNING_COLORS = [(255, 150, 190), (255, 215, 120), (150, 235, 190), (255, 180, 140), (150, 200, 255)]
WEEKEND_SCENE = True                         # weekends: a cozy room instead of the trains
WEEKEND_DAYS = (5, 6)                        # Saturday, Sunday (Mon=0)
STARS_FROM = 21                              # twinkling night-sky stars from 9pm until NIGHT_END

# Hardware
WIRING = "regular"        # "regular" = jumper wires straight from the Pi (see README);
                          # "adafruit-hat" if you later add the Adafruit bonnet or HAT
GPIO_SLOWDOWN = 3         # Pi 3B+: 3 (raise to 4 if the image shimmers); Pi 4: 4; Pi Zero 2: 2

PANEL_W, HEIGHT = 64, 32
TRAIN_ROWS = (12, 23)                 # top of the L and M rows (text is 7 pixels tall)
ROW_L_BOTTOM, ROW_M_BOTTOM = 18, 29   # their bottom lines; the right panel lines up to these
WIDTH = PANEL_W * 2
RIGHT_X = PANEL_W + 3     # left edge of the right-panel content
LEFT_X, TOP_Y = 2, 2      # margin from the board's left and top edges

# Colors (pastel)
WHITE = (230, 230, 230)
DIM = (150, 145, 170)       # lavender gray
PINK = (255, 150, 190)      # "MANHATTAN"
LAVENDER = (170, 150, 225)  # clock
L_GRAY = (200, 198, 218)    # L bullet (a soft, lavender-tinted take on the official gray)
M_ORANGE = (255, 150, 85)   # M bullet (a soft take on the official orange)
GREEN = (110, 235, 170)     # mint: you have time
YELLOW = (255, 205, 90)     # butter: leave soon
RED = (255, 80, 125)        # berry: leave now (blinks)
RED_DIM = (100, 25, 50)
BLUE = (130, 190, 255)
ICY = (190, 225, 255)
PEACH = (255, 175, 130)     # today's high
CREAM = (255, 238, 215)     # current temperature
SOFT_LAV = (205, 195, 235)  # today's high
SPOOKY_ORANGE = (255, 140, 30)
SPOOKY_PURPLE = (170, 100, 230)
XMAS_RED, XMAS_GREEN = (235, 60, 60), (60, 200, 90)
RAINBOW = [(255, 110, 140), (255, 180, 80), (255, 230, 110), (110, 235, 170),
           (130, 190, 255), (180, 150, 255)]
LOVE = [(255, 90, 130), PINK, (255, 200, 220)]
GOLD = (255, 200, 60)
SHAMROCK = [(60, 200, 90), (140, 235, 140)]
USA = [(240, 60, 70), (235, 235, 235), (80, 130, 255)]
HARVEST = [SPOOKY_ORANGE, (200, 120, 60), GOLD]
PASTEL = [(255, 170, 200), (255, 230, 150), (170, 230, 190), (180, 200, 255), (210, 180, 255)]
SPOOKY = [SPOOKY_ORANGE, SPOOKY_PURPLE]
XMAS = [XMAS_RED, XMAS_GREEN]
THEME_COLORS = {   # "MANHATTAN" letters and the scrolling message, cycled per letter
    "spooky": SPOOKY, "halloween": SPOOKY, "xmas": XMAS, "christmas": XMAS,
    "birthday": RAINBOW, "newyear": [GOLD, (255, 240, 180)], "anniversary": LOVE,
    "valentine": LOVE, "stpatricks": SHAMROCK,
    "easter": PASTEL, "july4": USA, "thanksgiving": HARVEST,
}

# Sprite palette: one character per color
PAL = {
    "W": (255, 232, 185),   # Korilakkuma's cream fur
    "e": (125, 85, 55),     # her soft brown outline
    "w": (255, 255, 250),   # muzzle and belly
    "t": (70, 45, 35),      # nose
    "p": (255, 150, 185),   # inner ear
    "c": (255, 120, 150),   # blush
    "r": (235, 40, 50),     # chest button
    "b": BLUE,              # nightcap, scarf, umbrella glyph
    "B": (70, 120, 235),    # scarf stripe
    "y": (255, 220, 110),   # pom-pom
    "h": (255, 160, 200),   # heart
    "u": (255, 140, 190),   # umbrella
    "U": (255, 230, 130),   # umbrella stripe
    "l": (170, 170, 180),   # umbrella pole
    "z": (255, 215, 130),   # sleepy z (butter yellow)
    "k": (255, 130, 175),   # beanie
    "K": (215, 80, 140),    # beanie rib
    "s": (255, 255, 255),   # star / snowflake
    "i": ICY,               # snowflake glyph
    "g": (215, 215, 238),   # cloud (soft lavender white)
    "Y": (255, 205, 90),    # sun, lightning
    "m": (255, 235, 165),   # moon
    "d": (140, 200, 255),   # raindrop
    "C": (205, 135, 80),    # Chairoikoguma fur
    "n": (255, 236, 205),   # Chairoikoguma muzzle
    "E": (90, 50, 25),      # Chairoikoguma outline
    "D": (110, 60, 30),     # Chairoikoguma closed eyes
    "R": (230, 40, 50),     # Santa red
    "x": (50, 45, 55),      # Santa belt and boots
    "V": (140, 80, 200),    # witch hat
    "O": (255, 140, 30),    # pumpkin / hat band
    "q": (255, 175, 60),    # pumpkin glow (flicker)
    "G": (90, 180, 90),     # pumpkin stem
    "v": (150, 90, 220),    # bat
    "M": (110, 235, 170),   # mint confetti
    "o": (255, 170, 120),   # sun rays (peach)
    "a": (205, 85, 140),    # couch outline
    "A": (255, 140, 190),   # couch (pink)
    "j": (255, 175, 210),   # couch cushion edge
    "J": (255, 205, 228),   # couch cushion
    "H": (230, 110, 160),   # blanket check / plant pot rim
    "L": (255, 215, 120),   # TV frame (butter yellow)
    "f": (245, 205, 220),   # table, TV stand (pale pink)
    "P": (240, 185, 105),   # pancakes
    "S": (170, 105, 50),    # syrup
}

# ─── 5x7 pixel font ──────────────────────────────────────────────────────────
_F = """
0 .###. #...# #..## #.#.# ##..# #...# .###.
1 ..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.
2 .###. #...# ....# ...#. ..#.. .#... #####
3 ##### ...#. ..#.. ...#. ....# #...# .###.
4 ...#. ..##. .#.#. #..#. ##### ...#. ...#.
5 ##### #.... ####. ....# ....# #...# .###.
6 ..##. .#... #.... ####. #...# #...# .###.
7 ##### ....# ...#. ..#.. .#... .#... .#...
8 .###. #...# #...# .###. #...# #...# .###.
9 .###. #...# #...# .#### ....# ...#. .##..
A .###. #...# #...# ##### #...# #...# #...#
B ####. #...# #...# ####. #...# #...# ####.
C .###. #...# #.... #.... #.... #...# .###.
D ###.. #..#. #...# #...# #...# #..#. ###..
E ##### #.... #.... ####. #.... #.... #####
F ##### #.... #.... ####. #.... #.... #....
G .###. #...# #.... #.### #...# #...# .####
H #...# #...# #...# ##### #...# #...# #...#
I .###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.
J ..### ...#. ...#. ...#. ...#. #..#. .##..
K #...# #..#. #.#.. ##... #.#.. #..#. #...#
L #.... #.... #.... #.... #.... #.... #####
M #...# ##.## #.#.# #.#.# #...# #...# #...#
N #...# #...# ##..# #.#.# #..## #...# #...#
O .###. #...# #...# #...# #...# #...# .###.
P ####. #...# #...# ####. #.... #.... #....
Q .###. #...# #...# #...# #.#.# #..#. .##.#
R ####. #...# #...# ####. #.#.. #..#. #...#
S .#### #.... #.... .###. ....# ....# ####.
T ##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..
U #...# #...# #...# #...# #...# #...# .###.
V #...# #...# #...# #...# #...# .#.#. ..#..
W #...# #...# #...# #.#.# #.#.# #.#.# .#.#.
X #...# #...# .#.#. ..#.. .#.#. #...# #...#
Y #...# #...# .#.#. ..#.. ..#.. ..#.. ..#..
Z ##### ....# ...#. ..#.. .#... #.... #####
! # # # # # . #
: . . # . # . .
- ... ... ... ### ... ... ...
. . . . . . . #
, .. .. .. .. .. .# #.
' # # . . . . .
/ ....# ...#. ...#. ..#.. .#... .#... #....
& .#.. #.#. #.#. .#.. #.## #..# .##.
( .# #. #. #. #. #. .#
) #. .# .# .# .# .# #.
+ ... ... .#. ### .#. ... ...
? .##. #..# ...# ..#. .#.. .... .#..
♥ ..... .#.#. ##### ##### .###. ..#.. .....
° .#. #.# .#. ... ... ... ...
a ..... ..... .###. ....# .#### #...# .####
b #.... #.... #.##. ##..# #...# #...# ####.
c ..... ..... .###. #.... #.... #...# .###.
d ....# ....# .##.# #..## #...# #...# .####
e ..... ..... .###. #...# ##### #.... .###.
f ..##. .#..# .#... ###.. .#... .#... .#...
g ..... ..... .#### #...# .#### ....# .###.
h #.... #.... #.##. ##..# #...# #...# #...#
i ..#.. ..... .##.. ..#.. ..#.. ..#.. .###.
j ...#. ..... ..##. ...#. ...#. #..#. .##..
k #.... #.... #..#. #.#.. ##... #.#.. #..#.
l .#... .#... .#... .#... .#... .#... ..##.
m ..... ..... ##.#. #.#.# #.#.# #...# #...#
n ..... ..... #.##. ##..# #...# #...# #...#
o ..... ..... .###. #...# #...# #...# .###.
p ..... ..... ####. #...# ####. #.... #....
q ..... ..... .#### #...# .#### ....# ....#
r ..... ..... #.##. ##..# #.... #.... #....
s ..... ..... .###. #.... .###. ....# ####.
t .#... .#... ###.. .#... .#... .#..# ..##.
u ..... ..... #...# #...# #...# #..## .##.#
v ..... ..... #...# #...# #...# .#.#. ..#..
w ..... ..... #...# #...# #.#.# #.#.# .#.#.
x ..... ..... #...# .#.#. ..#.. .#.#. #...#
y ..... ..... #...# #...# .#### ....# .###.
z ..... ..... ##### ...#. ..#.. .#... #####
"""
FONT = {line[0]: line[2:].split() for line in _F.strip("\n").splitlines()}
FONT[" "] = ["..."] * 7

# 8x8 icons. '#' = main color, 'o' = accent color.
L_BULLET = ["..####..", ".######.", "###.####", "###.####",
            "###.####", "###...##", ".######.", "..####.."]
M_BULLET = ["..####..", ".######.", "#.###.##", "#..#..##",
            "#.#.#.##", "#.###.##", ".######.", "..####.."]
BULLETS = {"L": (L_BULLET, L_GRAY), "M": (M_BULLET, M_ORANGE)}

# ─── Sprites (characters map to PAL colors; '.' = off) ──────────────────────
# Kawaii weather: a smiling sun with blushy cheeks and a sleepy little cloud
SUN_A = ["...o...", "..YYY..", ".YYYYY.", "oYxYxYo", ".cYYYc.", "..YYY..", "...o..."]
SUN_B = ["o.....o", "..YYY..", ".YYYYY.", ".YxYxY.", ".cYYYc.", "..YYY..", "o.....o"]
MOON_PEEK = [".mmm.", "mmmmm", "mmmmm", "mmmmm"]   # a round moon, half-hidden by the cloud
MOON = ["..mmm...", ".mm.....", "mm......", "mm......", "mm......", ".mm.....", "..mmm..."]
CLOUD = ["...ggg...", ".ggggggg.", "gggxgxggg", ".gcgggcg."]
BOLT = ["...YY...", "..YY....", "...Y....", "..Y....."]

UMBRELLA_GLYPH = ["..bbb..", ".bbbbb.", "bbbbbbb", "...b...", "...b...", "...b.b.", "....b.."]
SNOWFLAKE_GLYPH = ["...i...", ".i.i.i.", "..iii..", "iiiiiii", "..iii..", ".i.i.i.", "...i..."]
UMBRELLA_SMALL = [".bbb.", "bbbbb", "..b..", "..b..", ".bb.."]
SNOWFLAKE_SMALL = ["i.i.i", ".iii.", "iiiii", ".iii.", "i.i.i"]

BEAR = [            # Korilakkuma: cream fur, brown outline, white muzzle, red button
    "..eee........eee..",
    ".eppWe......eWppe.",
    "eppWWeeeeeeeeWWppe",
    "epWWWWWWWWWWWWWWpe",
    "eWWWWWWWWWWWWWWWWe",
    "eWWWWWWWWWWWWWWWWe",
    "eWWW..WWWWWW..WWWe",
    "eWWW..WwwwwW..WWWe",
    "eWWWWWwwttwwWWWWWe",
    ".eWWWWwwrrwwWWWWe.",
    "..eeWWWwwwwWWWee..",
    "....eeeeeeeeee....",
    "...eWWWWWWWWWWe...",
    ".eWWWwwwwwwwwWWWe.",
    "eWWeWwwwrrwwwWeWWe",
    ".ee.eWwwwwwwWe.ee.",
    "....eWWWWWWWWe....",
    "...epWe....eWpe...",
]
BEAR_X, BEAR_Y = 108, 14
# Face landmarks on BEAR, used for blinking, sleeping and yawning
EYES = [(4, 6), (5, 6), (4, 7), (5, 7), (12, 6), (13, 6), (12, 7), (13, 7)]
# accessories, drawn relative to the bear's top-left; first number = row offset
UMBRELLA = (-7, [".......uUUu.......",
                 ".....uuuUUuuu.....",
                 "...uuuuuUUuuuuu...",
                 "..uuuuuuUUuuuuuu..",
                 "..u.u.u.ll.u.u.u.."])
SCARF = (11, ["..bBbBbBbBbBbBbB..",
              "....BbBbBbBbBb....",
              "............bB....",
              ".............bB...",
              ".............Bb..."])
BEANIE = (-1, ["........yy........",
               ".....kkkkkkkk.....",
               "....kKkKkKkKkK....",
               "....KKKKKKKKKK...."])
NIGHTCAP = (-2, ["..............y...",
                 "..........bbbbb...",
                 "........bbbbbb....",
                 "......bbbbbbb.....",
                 "....bbbbbbbbbb...."])
HEART = [".h.h.", "hhhhh", ".hhh.", "..h.."]

# Chairoikoguma: the little brown buddy who sits beside the bear
CHAIRO = [          # brown fur, darker outline, little cream muzzle, white bib, yellow foot pads
    ".EE....EE.",
    "ECCEEEECCE",
    "ECCCCCCCCE",
    "EC.CCCC.CE",
    "ECCCnnCCCE",
    "ECCnttnCCE",
    ".ECCnnCCE.",
    "..ECCCCE..",
    ".EwwwwwwE.",
    ".ECwwwwCE.",
    "ECCCCCCCCE",
    "EyCCEECCyE",
    ".EEEEEEEE.",
]
CHAIRO_X, CHAIRO_Y = 97, 19
CHAIRO_UMBRELLA = (-5, ["...UUUU...",
                        ".UUUUUUUU.",
                        "UUUUUUUUUU",
                        "U.U.ll.U.U",
                        "....ll...."])
ZZZ = ["zzz", ".z.", "zzz"]

# Costumes (row offset relative to the bear's top-left, rows)
SANTA_HAT = (-3, ["..............ss..",
                  "...........RRRss..",
                  "........RRRRRR....",
                  "......RRRRRRRR....",
                  "....RRRRRRRRRR....",
                  "...ssssssssssss..."])
SANTA_SUIT = (11, ["....RRRRRRRRRR....",
                   "...RRRRRssRRRRR...",
                   "..RRRRRRssRRRRRR..",
                   ".xxxxxxxyyxxxxxxx.",
                   ".RRRRRRRssRRRRRRR.",
                   "..RRRRRRssRRRRRR..",
                   "..xxxxx....xxxxx.."])
WITCH_HAT = (-4, ["..........VV......",
                  ".........VVV......",
                  "........VVVV......",
                  ".......VVVVV......",
                  "......VVVVVV......",
                  ".....OOOOOOOO.....",
                  "VVVVVVVVVVVVVVVVVV"])
PARTY_HAT = (-4, ["........yy........",
                  "........uU........",
                  ".......uUuU.......",
                  ".......UuUu.......",
                  "......uUuUuU......",
                  "......UuUuUu......",
                  ".....uUuUuUuU....."])
GHOST = [   # Halloween: the bear in a bedsheet ghost costume
    ".....ssssssss.....",
    "...ssssssssssss...",
    "..ssssssssssssss..",
    ".ssssssssssssssss.",
    "ssssssssssssssssss",
    "ssss..ssssss..ssss",
    "ssss..ssssss..ssss",
    "ssccssssssssssccss",
    "ssssssss..ssssssss",
    "ssssssss..ssssssss",
    "ssssssssssssssssss",
    "ssssssssssssssssss",
    "ssssssssssssssssss",
    "ssssssssssssssssss",
    "ssssssssssssssssss",
    "ssssssssssssssssss",
    "ssssssssssssssssss",
    "ss..ss..ss..ss..ss",
]
PUMPKIN = [
    "....G....",
    ".OOOGOOO.",
    "OOOOOOOOO",
    "OOYOOOYOO",
    "OYYYOYYYO",
    "OOOOOOOOO",
    "OYOYOYOYO",
    ".OOOOOOO.",
]
PUMPKIN_FLICKER = [row.replace("Y", "q") for row in PUMPKIN]
BAT = [["v...v", ".vvv.", "..v.."], ["..v..", ".vvv.", "v...v"]]   # wings up / down
MUG = ["uDDu", "uuuu", "uuuuu", "uuuuu", "uuuu"]   # with a little handle   # hot coffee (brown on top)
SWEAT = ["d.", "dd", "dd"]
BUNNY_EARS = (-4, ["...WW........WW...",
                   "..WpW........WpW..",
                   "..WpW........WpW..",
                   "..WpW........WpW..",
                   "..WpW........WpW..",
                   "..WWW........WWW..",
                   "....hhhhhhhhhh...."])
SUNGLASSES = (6, ["..xxxxxxxxxxxxxx..",
                  "..xxxx......xxxx.."])
CROSS = ["..y..", "..y..", "yyyyy", "..y..", "..y..", "..y..", "..y..", "..y.."]
TREE = [            # Christmas tree; ornaments twinkle by swapping colors
    "....Y....",
    "....G....",
    "...GrG...",
    "..GGGGG..",
    "..GbGGU..",
    ".GGGGGGG.",
    ".GUGGrGG.",
    "GGGGGGGGG",
    "GrGGbGGUG",
    "....D....",
    "....D....",
]
HEART_BURSTS = [    # heart-shaped firework, growing
    ["h.h", "hhh", ".h."],
    [".h.h.", "h.h.h", "h...h", ".h.h.", "..h.."],
    [".hh.hh.", "h..h..h", "h.....h", ".h...h.", "..h.h..", "...h..."],
]
LOUNGE = [          # lazy weekend afternoon: lying on its belly, chin on paws
    ".WWW......WWW......",
    "WWpWWWWWWWWpWW.....",
    "WWWWWWWWWWWWWW.....",
    "WWWWWWWWWWWWWW.WWW.",
    "WWW.WWWWWW.WWWWWWWW",
    "WWW.WWWWWW.WWWWWWWW",
    "WWWWWwttwWWWWWWWWWW",
    ".WWWWwwwwWWWWWWWWWW",
    "WWWWWWWWWWWWWWWWWW.",
    "WWWW.WWWWWWWWWWWWW.",
]
# Falling things around the bear, one color per column
PARTICLES = {
    "snow": "ssss", "confetti": "hUbM", "petals": "phph",
    "leaves": "ORyO", "clovers": "GMGM", "hearts": "hrhc",
}


@functools.lru_cache(maxsize=None)
def _compile_sprite(rows):
    """Turn a text grid into a list of (dx, dy, color) once, so frames draw fast."""
    return [(dx, dy, PAL[ch]) for dy, row in enumerate(rows)
            for dx, ch in enumerate(row) if ch in PAL]


def draw_sprite(img, x, y, rows):
    px = img.load()
    for dx, dy, color in _compile_sprite(tuple(rows)):
        if 0 <= x + dx < WIDTH and 0 <= y + dy < HEIGHT:
            px[x + dx, y + dy] = color


def draw_dots(img, x, y, pts, ch):
    px = img.load()
    for dx, dy in pts:
        if 0 <= x + dx < WIDTH and 0 <= y + dy < HEIGHT:
            px[x + dx, y + dy] = PAL[ch] if ch else (0, 0, 0)


# Each glyph precomputed as (width, [(dx, dy), ...] of lit pixels)
GLYPHS = {c: (len(g[0]), [(dx, dy) for dy, row in enumerate(g)
                          for dx, bit in enumerate(row) if bit == "#"])
          for c, g in FONT.items()}

# Compact 3x5 font for the weather details and the clock
_SMALL = """
0 ### #.# #.# #.# ###
1 .#. ##. .#. .#. ###
2 ##. ..# .#. #.. ###
3 ##. ..# .#. ..# ##.
4 #.# #.# ### ..# ..#
5 ### #.. ##. ..# ##.
6 .## #.. ### #.# ###
7 ### ..# .#. #.. #..
8 ### #.# ### #.# ###
9 ### #.# ### ..# ##.
: . # . # .
- ... ... ### ... ...
° ##. ##. ... ... ...
H #.# #.# ### #.# #.#
L #.. #.. #.. #.. ###
N #..# ##.# #.## #..# #..#
O .#. #.# #.# #.# .#.
W #...# #...# #.#.# #.#.# .#.#.
h #.. #.. ##. #.# #.#
l #. #. #. #. .#
n ... ##. #.# #.# #.#
o ... .#. #.# #.# .#.
w ..... #...# #.#.# #.#.# .#.#.
M #...# ##.## #.#.# #...# #...#
I ### .#. .#. .#. ###
m ..... ####. #.#.# #.#.# #.#.#
i # . # # #
"""
SMALL_GLYPHS = {line[0]: (len(line[2:].split()[0]),
                          [(dx, dy) for dy, row in enumerate(line[2:].split())
                           for dx, bit in enumerate(row) if bit == "#"])
                for line in _SMALL.strip("\n").splitlines()}
SMALL_GLYPHS[" "] = (2, [])

# Tiny 3x5 capitals for Korilakkuma's speech bubble (any letter, a few marks)
_TINY = """
A .#. #.# ### #.# #.#
B ##. #.# ##. #.# ##.
C .## #.. #.. #.. .##
D ##. #.# #.# #.# ##.
E ### #.. ##. #.. ###
F ### #.. ##. #.. #..
G .## #.. #.# #.# .##
H #.# #.# ### #.# #.#
I ### .#. .#. .#. ###
J ..# ..# ..# #.# .#.
K #.# #.# ##. #.# #.#
L #.. #.. #.. #.. ###
M #...# ##.## #.#.# #...# #...#
N #..# ##.# #.## #..# #..#
O .#. #.# #.# #.# .#.
P ##. #.# ##. #.. #..
Q .#. #.# #.# ##. .##
R ##. #.# ##. #.# #.#
S .## #.. .#. ..# ##.
T ### .#. .#. .#. .#.
U #.# #.# #.# #.# ###
V #.# #.# #.# #.# .#.
W #...# #...# #.#.# #.#.# .#.#.
X #.# #.# .#. #.# #.#
Y #.# #.# .#. .#. .#.
Z ### ..# .#. #.. ###
. . . . . #
, . . . # #
' # # . . .
! # # # . #
? ### ..# .#. ... .#.
- ... ... ### ... ...
/ ..# ..# .#. #.. #..
: . # . # .
( .# #. #. #. .#
) #. .# .# .# #.
♥ #.# ### .#. ... ...
"""
TINY_GLYPHS = {line[0]: (len(line[2:].split()[0]),
                         [(dx, dy) for dy, row in enumerate(line[2:].split())
                          for dx, bit in enumerate(row) if bit == "#"])
               for line in _TINY.strip("\n").splitlines()}
for _d in "0123456789":
    TINY_GLYPHS[_d] = SMALL_GLYPHS[_d]
TINY_GLYPHS[" "] = (2, [])


def tiny_width(s):
    return sum(TINY_GLYPHS.get(c, TINY_GLYPHS[" "])[0] + 1 for c in s.upper()) - 1 if s else 0


def draw_tiny(img, x, y, s, color):
    px = img.load()
    for c in s.upper():
        w, pts = TINY_GLYPHS.get(c, TINY_GLYPHS[" "])
        for dx, dy in pts:
            if 0 <= x + dx < WIDTH and 0 <= y + dy < HEIGHT:
                px[x + dx, y + dy] = color
        x += w + 1
    return x


def wrap_tiny(text, width):
    """Split text into lines that fit `width` pixels in the tiny font."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if tiny_width(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines
FONTS = {"big": (GLYPHS, 7), "small": (SMALL_GLYPHS, 5)}


def _case(s):
    return s.lower() if LETTER_CASE == "lower" else s.upper()


def text_width(s, font="big"):
    glyphs = FONTS[font][0]
    return sum(glyphs.get(c, glyphs[" "])[0] + 1 for c in _case(s)) - 1 if s else 0


def draw_text(img, x, y, s, color, clip=(0, WIDTH), rows=(0, HEIGHT), font="big"):
    """Draw text; color may be a list, cycled per letter. clip/rows limit the drawn area.
    font: "big" (5x7) or "small" (3x5)."""
    px = img.load()
    lo, hi = clip
    top, bottom = rows
    glyphs, gh = FONTS[font]
    colors = color if isinstance(color, list) else None
    for i, c in enumerate(_case(s)):
        w, pts = glyphs.get(c, glyphs[" "])
        if x >= hi:
            break
        col = colors[i % len(colors)] if colors else color
        if x + w > lo:
            inside = x >= lo and x + w <= hi and y >= top and y + gh <= bottom
            for dx, dy in pts:
                if inside or (lo <= x + dx < hi and top <= y + dy < bottom):
                    px[x + dx, y + dy] = col
        x += w + 1
    return x


def draw_icon(img, x, y, rows, color, accent=None):
    px = img.load()
    for dy, row in enumerate(rows):
        for dx, bit in enumerate(row):
            if bit == "#" or (bit == "o" and accent):
                px[x + dx, y + dy] = color if bit == "#" else accent

# ─── Data fetching ───────────────────────────────────────────────────────────
def fetch_arrivals(line):
    """Upcoming trains at the line's stop, plus how far our clock is off from the MTA's.

    Returns ([(arrival_unix_time, trip_id, on_its_way), ...], clock_skew_seconds).
    on_its_way is False for trains that haven't left their starting terminal yet:
    their times come from the timetable, and occasionally those trains never run."""
    sent = time.time()
    resp = requests.get(line["feed"], timeout=10)
    resp.raise_for_status()
    skew = 0.0
    if "Date" in resp.headers:
        server = parsedate_to_datetime(resp.headers["Date"]).timestamp()
        skew = server - (sent + time.time()) / 2
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    now = time.time() + skew
    trains = []
    for entity in feed.entity:
        tu = entity.trip_update
        if not entity.HasField("trip_update") or tu.trip.route_id != line["route"]:
            continue
        stops = tu.stop_time_update
        if not stops:
            continue
        first = stops[0]
        first_t = first.departure.time or first.arrival.time
        on_its_way = first.stop_id not in line["origins"] or first_t <= now + 30
        for stu in stops:
            if stu.stop_id == line["stop"]:
                t = stu.arrival.time or stu.departure.time
                if t:
                    trains.append((t, tu.trip.trip_id, on_its_way))
    return sorted(trains), skew


def fetch_alerts(now):
    """{route: [alert texts]} for L/M problems on your way to Manhattan, active now.
    Only shown on the board when a line has no catchable trains, to explain why."""
    data = requests.get(ALERTS_URL, timeout=10).json()
    lines = {ln["route"]: ln for ln in LINES}
    out = {}
    for ent in data.get("entity", []):
        al = ent.get("alert", {})
        merc = al.get("transit_realtime.mercury_alert", {})
        early = merc.get("display_before_active", 0)
        periods = al.get("active_period", [])
        if periods and not any(p.get("start", 0) - early <= now <= (p.get("end") or 9e12)
                               for p in periods):
            continue
        routes = set()
        for ie in al.get("informed_entity", []):
            ln = lines.get(ie.get("route_id"))
            stop = ie.get("stop_id")
            if ln and (not stop or ln["trip_stop"](stop)):
                routes.add(ln["route"])
        text = next((t["text"] for t in al.get("header_text", {}).get("translation", [])
                     if t.get("language") == "en"), "")
        text = " ".join(text.replace("[", "").replace("]", "").split())
        for r in routes:
            if text and text not in out.setdefault(r, []):
                out[r].append(text)
    return out


def fetch_weather():
    w = requests.get(weather_url(), timeout=10).json()
    cur, daily, hourly = w["current"], w["daily"], w["hourly"]
    # first hour in the lookahead window with a real chance of precipitation
    precip = None
    now_hour = cur["time"][:13]
    limit = _add_hours(now_hour, RAIN_LOOKAHEAD_H)
    for t, p, code in zip(hourly["time"], hourly["precipitation_probability"],
                          hourly["weather_code"]):
        if t[:13] < now_hour:
            continue
        if t[:13] > limit:
            break
        if (p or 0) >= RAIN_CHANCE:
            precip = (t, kind_of(code) or "rain")
            break
    return dict(
        temp=round(cur["temperature_2m"]),
        icon=icon_for(cur["weather_code"], cur["is_day"]),
        hi=round(daily["temperature_2m_max"][0]),
        lo=round(daily["temperature_2m_min"][0]),
        now_kind=kind_of(cur["weather_code"]),
        precip=precip,
    )


def _add_hours(hour_str, h):
    t = datetime.strptime(hour_str, "%Y-%m-%dT%H").timestamp() + h * 3600
    return datetime.fromtimestamp(t).strftime("%Y-%m-%dT%H")


def kind_of(code):
    """WMO weather code → 'rain', 'snow', 'storm' or None."""
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    return None


def icon_for(code, is_day):
    kind = kind_of(code)
    if kind:
        return kind
    if code in (0, 1):
        return "sun" if is_day else "moon"
    return "cloud" if is_day else "cloud_night"


class State:
    """Latest data from every source, updated by a background thread."""
    def __init__(self):
        self.lock = threading.Lock()
        self.arrivals = {ln["route"]: [] for ln in LINES}
        self.trains_ok = {ln["route"]: 0.0 for ln in LINES}
        self.alerts, self.weather, self.weather_ok = {}, None, 0.0
        self.clock_fix = 0.0                         # seconds to add to our clock
        self._shown = {ln["route"]: {} for ln in LINES}   # trip_id → time we're displaying
        self._last = {"trains": 0.0, "alerts": 0.0, "weather": 0.0}

    def now(self):
        return time.time() + self.clock_fix

    def refresh_due(self, force=False):
        now = time.time()
        if force or now - self._last["trains"] >= TRAINS_EVERY_S:
            self._last["trains"] = now
            for ln in LINES:
                self._try(f"{ln['route']} train", lambda: self._set_trains(ln))
        if force or now - self._last["alerts"] >= ALERTS_EVERY_S:
            self._last["alerts"] = now
            self._try("alerts", self._set_alerts)
        if force or now - self._last["weather"] >= WEATHER_EVERY_S:
            self._last["weather"] = now
            self._try("weather", self._set_weather)

    def _set_trains(self, ln):
        trains, skew = fetch_arrivals(ln)
        fix = skew if abs(skew) > CLOCK_FIX_AFTER_S else 0.0
        if (fix == 0.0) != (self.clock_fix == 0.0):
            print(f"[{datetime.now():%H:%M:%S}] clock is off by {skew:+.0f}s vs the MTA; "
                  f"{'correcting' if fix else 'back in sync'}", file=sys.stderr)
        # Steady times: keep showing a train's previous prediction unless it really moved
        old, shown, steady = self._shown[ln["route"]], {}, []
        for t, trip, on_its_way in trains:
            if trip in old and abs(t - old[trip]) < SMOOTH_S:
                t = old[trip]
            shown[trip] = t
            steady.append((t, trip, on_its_way))
        with self.lock:
            self.clock_fix = fix
            self._shown[ln["route"]] = shown
            self.arrivals[ln["route"]] = sorted(steady)
            self.trains_ok[ln["route"]] = self.now()

    def _set_alerts(self):
        a = fetch_alerts(self.now())
        with self.lock:
            self.alerts = a

    def _set_weather(self):
        w = fetch_weather()
        with self.lock:
            self.weather, self.weather_ok = w, self.now()

    @staticmethod
    def _try(name, fn):
        try:
            fn()
        except Exception as e:  # network blip, MTA hiccup, bad payload — keep last data
            print(f"[{datetime.now():%H:%M:%S}] {name} fetch failed: {e}", file=sys.stderr)

    def run_forever(self):
        while True:
            self.refresh_due()
            time.sleep(1)

    def snapshot(self):
        with self.lock:
            return (dict(self.arrivals), dict(self.trains_ok), dict(self.alerts),
                    self.weather, self.weather_ok)

# ─── Your own settings ───────────────────────────────────────────────────────
# Anything in my_settings.py (stations, location, special days, countdowns, messages,
# schedule...) replaces the defaults above. Keep it private; see my_settings.example.py.
try:
    from my_settings import *   # noqa: F401,F403
except ImportError:
    pass

# ─── Rendering ───────────────────────────────────────────────────────────────
def catchable(arrivals, now, walk_min, n=2):
    """[(min_until_arrival, min_until_you_must_leave, on_its_way)] for trains you can make."""
    out = []
    for t, _trip, on_its_way in arrivals:
        leave_s = t - now - walk_min * 60
        if leave_s >= 0:
            out.append((int((t - now) // 60), int(leave_s // 60), on_its_way))
    return out[:n]


def urgency_color(leave_min, now, on_its_way=True):
    if leave_min == 0:
        c = RED if int(now * 2) % 2 == 0 else RED_DIM   # blink twice a second
    else:
        c = YELLOW if leave_min <= YELLOW_AT_MIN else GREEN
    if not on_its_way:   # still at its terminal: timetable guess, so show it dimmer
        c = tuple(v * 9 // 20 for v in c)
    return c


# ── Calendar: which holiday / season is it? ──
def easter(year):
    """Gregorian Easter Sunday (the standard 'anonymous' algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (b - (b + 8) // 25 + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1


def thanksgiving(year):
    """Fourth Thursday of November."""
    first = datetime(year, 11, 1).weekday()          # Monday = 0
    return 11, 1 + (3 - first) % 7 + 21


def day_theme(dt):
    """(theme, message) for today's special day, or (None, None)."""
    md = dt.strftime("%m-%d")
    if md in PERSONAL_DAYS:
        return PERSONAL_DAYS[md]
    if md in HOLIDAYS:
        return HOLIDAYS[md]
    if (dt.month, dt.day) == easter(dt.year):
        return "easter", "HAPPY EASTER!"
    if (dt.month, dt.day) == thanksgiving(dt.year):
        return "thanksgiving", "HAPPY THANKSGIVING!"
    return None, None


def season_theme(dt):
    md = (dt.month, dt.day)
    if dt.month == 10:
        return "spooky"
    if dt.month == 12:
        return "xmas"
    if (3, 20) <= md <= (5, 31):
        return "spring"
    if (6, 21) <= md <= (9, 21):
        return "summer"
    if (9, 22) <= md <= (11, 30):
        return "fall"
    return None


def is_night(now):
    dt = datetime.fromtimestamp(now)
    wake = WEEKEND_WAKE if dt.weekday() >= 5 else NIGHT_END
    return dt.hour >= NIGHT_START or dt.hour < wake


def bear_mood(wx, precip_kind, now):
    if is_night(now):
        return "sleep"
    if precip_kind == "snow":
        return "snow"
    if precip_kind in ("rain", "storm"):
        return "umbrella"
    if wx and wx["temp"] < COLD_F:
        return "cold"
    return "idle"


# ── Sliding numbers ──
_slides = {}


def draw_sliding(img, key, x, y, text, color, now, font="big"):
    """Draw text; when it changes, the old text slides up and out as the new one slides in.
    Returns the x position just after the text (like draw_text)."""
    s = _slides.get(key)
    if s is None or not (0 <= now - s["seen"] <= 2):   # first sighting or a time jump: no slide
        s = _slides[key] = dict(text=text, x=x, prev=None, px=x, t0=-1e18, seen=now)
    elif text != s["text"]:
        s.update(prev=s["text"], px=s["x"], text=text, t0=now)
    s["x"], s["seen"] = x, now
    h = FONTS[font][1] + 1
    band = (y, y + h)
    p = (now - s["t0"]) / SLIDE_S
    prev = s["prev"]
    kw = dict(rows=band, font=font)
    if prev is None or not 0 <= p < 1:
        draw_text(img, x, y, text, color, **kw)
    else:
        d = int(h * p)
        if len(prev) == len(text) and s["px"] == x:
            # same layout: only the characters that changed slide (like a flip clock)
            cx = x
            for old_c, new_c in zip(prev, text):
                if old_c == new_c:
                    draw_text(img, cx, y, new_c, color, **kw)
                else:
                    draw_text(img, cx, y - d, old_c, color, **kw)
                    draw_text(img, cx, y + h - d, new_c, color, **kw)
                cx += text_width(new_c, font) + 1
        else:
            draw_text(img, s["px"], y - d, prev, color, **kw)
            draw_text(img, x, y + h - d, text, color, **kw)
    return x + text_width(text, font) + 1


BUBBLE = (66, 0, 62, 9)       # Korilakkuma's speech bubble: x, y, width, height (over the weather row)


def draw_speech(img, f, line, lounging=False):
    """A pink speech bubble above Korilakkuma with the current line of her message."""
    if not line:
        return
    px = img.load()
    bx, by, bw, bh = BUBBLE
    for yy in range(by, by + bh):
        for xx in range(bx, bx + bw):
            edge = xx in (bx, bx + bw - 1) or yy in (by, by + bh - 1)
            corner = (xx in (bx, bx + bw - 1)) and (yy in (by, by + bh - 1))
            px[xx, yy] = (0, 0, 0) if corner else ((255, 160, 200) if edge else (16, 5, 12))
    tip_x = BEAR_X + 9 if not lounging else BEAR_X + 4      # the tail points at her head
    for k, (dx, dy) in enumerate(((-1, 0), (0, 0), (0, 1), (1, 2))):
        px[tip_x + dx, by + bh - 1 + dy] = (16, 5, 12) if (k == 1 and dy == 0) else (255, 160, 200)
    draw_tiny(img, bx + (bw - tiny_width(line)) // 2, by + 2, line, CREAM)
    if not lounging and (f // 2) % 2:                       # her mouth moves while she talks
        draw_dots(img, BEAR_X, BEAR_Y, [(8, 9), (9, 9)], None)


def draw_hilo(img, x, y, hi, lo, colors=None):
    """Today's high and low as "62/48": the high in soft lavender, the low a shade dimmer."""
    c_hi, c_slash, c_lo = colors or (SOFT_LAV, (110, 100, 140), DIM)
    x = draw_text(img, x, y, f"{hi}°", c_hi)
    x = draw_text(img, x, y, "/", c_slash)
    draw_text(img, x, y, f"{lo}°", c_lo)


def draw_clock(img, x, y, dt, now):
    """24-hour clock in pink with a tiny heart for the colon that beats once a second."""
    x = draw_sliding(img, "clock_h", x, y, dt.strftime("%H"), PINK, now)
    beat = now % 1 < 0.25                                  # the heartbeat: a quick bright pulse
    color = (255, 90, 150) if beat else (255, 150, 195)
    px = img.load()
    for dx, dy in [(0, 2), (2, 2), (0, 3), (1, 3), (2, 3), (1, 4)]:   # a 3x3 heart
        px[x + dx, y + dy] = color
    draw_sliding(img, "clock_m", x + 4, y, dt.strftime("%M"), PINK, now)


def temp_color(t):
    """Pastel color for a temperature: icy blue when freezing up to peach when hot."""
    for limit, color in ((33, (170, 210, 255)), (50, (165, 235, 225)),
                         (65, (205, 240, 175)), (80, (255, 220, 140))):
        if t < limit:
            return color
    return PEACH


# ── The board ──
def _surprise_seed():
    """A secret shuffle seed: this machine's id, so the order on the Pi isn't one you
    (or a preview on the Mac) could know ahead of time."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as fh:
                return fh.read().strip()
        except OSError:
            pass
    return "train-board"


def morning_message(dt):
    """Today's message: a secret shuffle of the list, one per day, none repeating until all have shown."""
    order = MORNING_MESSAGES[:]
    random.Random(_surprise_seed()).shuffle(order)
    return order[dt.toordinal() % len(order)]


def countdown_lines(dt):
    """Countdown messages for anything in COUNTDOWNS coming up within COUNTDOWN_WITHIN days."""
    out, today = [], dt.date()
    for name, when in COUNTDOWNS:
        if len(when) == 5:                                # "12-25": every year
            target = datetime.strptime(f"{today.year}-{when}", "%Y-%m-%d").date()
            if target < today:
                target = target.replace(year=today.year + 1)
        else:                                             # "2026-12-27": one time only
            target = datetime.strptime(when, "%Y-%m-%d").date()
        days = (target - today).days
        if days == 0:
            out.append(f"today: {name}! ♥")
        elif 0 < days <= COUNTDOWN_WITHIN:
            out.append(f"{days} {'day' if days == 1 else 'days'} until {name}{'!!' if days <= 7 else '!'}")
    return out


def bubble_text(dt, now):
    """What Korilakkuma says this time: today's message, alternating with any countdowns."""
    n = int((now - 30) // MESSAGE_EVERY_S)
    counts = countdown_lines(dt)
    if counts and n % 2:
        return counts[(n // 2) % len(counts)]
    return morning_message(dt)


def morning_page(dt, now, width):
    """The line of today's message to show right now (it's said a few words at a time,
    once every MESSAGE_EVERY_S until noon), or None when nobody's talking."""
    if dt.hour >= MORNING_UNTIL:
        return None
    lines = wrap_tiny(bubble_text(dt, now), width)
    t = (now - 30) % MESSAGE_EVERY_S            # starts 30 s into the cycle (holiday messages use :00)
    k = int(t / PAGE_S)
    return lines[k] if k < len(lines) else None


def screen_off(dt):
    """True outside the on-hours (WEEKDAY_ON/OFF, WEEKEND_ON/OFF): the board goes dark."""
    def mins(hhmm):
        h, m = map(int, hhmm.split(":"))
        return h * 60 + m
    t = dt.hour * 60 + dt.minute
    weekend = dt.weekday() in WEEKEND_DAYS
    yesterday_weekend = (dt.weekday() - 1) % 7 in WEEKEND_DAYS
    on, off = (WEEKEND_ON, WEEKEND_OFF) if weekend else (WEEKDAY_ON, WEEKDAY_OFF)
    on, off = mins(on), mins(off)
    if on < off:                                   # e.g. 07:00-10:00
        lit = on <= t < off
    else:                                          # runs past midnight, e.g. 09:00-01:00
        lit = t >= on
    # the small hours belong to the night before (Sunday 00:30 is still Saturday night)
    y_on, y_off = map(mins, (WEEKEND_ON, WEEKEND_OFF) if yesterday_weekend else (WEEKDAY_ON, WEEKDAY_OFF))
    if y_off < y_on and t < y_off:
        lit = True
    return not lit


def render(snap, now):
    arrivals, trains_ok, alerts, weather, weather_ok = snap
    img = Image.new("RGB", (WIDTH, HEIGHT))
    if screen_off(datetime.fromtimestamp(now + DISPLAY_SHIFT)):
        return img                                  # lights out: an all-black frame
    dt = datetime.fromtimestamp(now + DISPLAY_SHIFT)   # --pretend moves only the calendar/clock
    day, message = day_theme(dt)
    season = season_theme(dt)
    f = int(now * 10)                          # animation frame (10 per second)

    # While it's actually raining or snowing, faint drops/flakes fall across the whole
    # board, behind everything (text and bears are drawn on top of them)
    if weather and now - weather_ok <= WEATHER_STALE_S and weather["now_kind"]:
        draw_weather_backdrop(img, f, weather["now_kind"])

    # Left panel: trains (on weekends a cozy room is drawn there instead, further down)
    weekend = WEEKEND_SCENE and dt.weekday() in WEEKEND_DAYS
    header = THEME_COLORS.get(day) or THEME_COLORS.get(season) or PINK
    if not weekend:
        draw_text(img, (PANEL_W - text_width(HEADER)) // 2, TOP_Y, HEADER, header)
    hurry = False
    for i, ln in enumerate(LINES if not weekend else []):
        y = TRAIN_ROWS[i]
        bullet, color = BULLETS[ln["route"]]
        draw_icon(img, LEFT_X, y, bullet, color)
        x = LEFT_X + 11
        if now - trains_ok[ln["route"]] > TRAINS_STALE_S:
            draw_text(img, x, y, "NO DATA", RED_DIM)
            continue
        trains = catchable(arrivals[ln["route"]], now, ln["walk_min"])
        if not trains:
            line_alerts = alerts.get(ln["route"])
            if line_alerts:   # explain the empty row by scrolling the MTA's alert in it
                draw_scroll(img, x, PANEL_W, y, "   +   ".join(line_alerts), YELLOW, now)
            else:
                draw_text(img, x, y, "NONE SOON", DIM)
        for j, (arrive, leave, on_its_way) in enumerate(trains):
            x = draw_sliding(img, (ln["route"], j), x, y, str(arrive),
                             urgency_color(leave, now, on_its_way), now) + 4
            if j == len(trains) - 1:                       # a small, quiet "min" at the end
                draw_text(img, x - 2, y + 2, "MIN", DIM, font="small")
            hurry = hurry or (leave == 0 and on_its_way)

    # Right panel: weather, message, rain/snow warning, clock
    x0 = RIGHT_X
    clock = dt.strftime("%H:%M")
    wx = weather if weather and now - weather_ok <= WEATHER_STALE_S else None
    precip_kind = None
    when = ""
    if wx:
        if wx["now_kind"]:
            precip_kind, when = wx["now_kind"], "NOW"
        elif wx["precip"]:
            precip_kind, when = wx["precip"][1], wx["precip"][0][11:13] + "h"   # "16h" (fits beside Chairo)
    if not weekend:
        if wx:
            draw_weather_icon(img, x0, TOP_Y, wx["icon"], f)
            draw_sliding(img, "temp", x0 + 11, TOP_Y, f"{wx['temp']}°", CREAM, now)
        else:
            draw_text(img, x0, TOP_Y, "--°", DIM)

    # The right panel's rows sit on the same lines as the left panel's:
    #   temperature ↔ "manhattan", middle row ↔ L train row, clock ↔ M train row.
    row_b, row_c = TRAIN_ROWS         # same big text, same lines as the L and M rows
    scroll_s = (text_width(message) + 44) / SCROLL_PX_PER_S if message else 0
    if weekend:
        pass                          # the weekend room draws its own info board
    elif message and now % 60 < scroll_s:
        # special days: the message scrolls through the middle row once a minute
        span_end = BEAR_X - 3
        offset = int((now % 60) * SCROLL_PX_PER_S)
        draw_text(img, span_end - offset, ROW_L_BOTTOM - 6, message,
                  THEME_COLORS.get(day, PINK), clip=(x0, span_end))
    elif precip_kind:   # rain/snow coming: that's more useful than the high/low
        snowy = precip_kind == "snow"
        draw_sprite(img, x0, row_b + 1, SNOWFLAKE_SMALL if snowy else UMBRELLA_SMALL)
        draw_text(img, x0 + 7, row_b, when, ICY if snowy else BLUE)
    elif wx:
        draw_hilo(img, x0, row_b, wx["hi"], wx["lo"])
    if not weekend:
        draw_clock(img, x0, row_c, dt, now)

    # The bear and friends
    mood = bear_mood(wx, precip_kind, now + DISPLAY_SHIFT)
    theme = day or season
    rainy = precip_kind in ("rain", "storm")
    visit = clock in BUDDY_TIMES or day in ("anniversary", "valentine")
    buddy = rainy or visit
    hurry = hurry and mood != "sleep"
    lazy = None   # weekends: hot coffee in the morning, lounging in the afternoon
    if dt.weekday() >= 5 and mood in ("idle", "cold") and not hurry:
        lazy = "coffee" if dt.hour < COFFEE_UNTIL else "lounge"
    hot = bool(wx and wx["temp"] >= HOT_F)
    christmassy = theme in ("xmas", "christmas")
    if christmassy:
        draw_lights(img, f)
    fireworks = theme in ("july4", "newyear", "valentine", "anniversary")
    if (dt.hour >= STARS_FROM or dt.hour < NIGHT_END) and not fireworks \
            and not (wx and wx["now_kind"]):
        draw_stars(img, now)
    if weekend:
        draw_room(img, f, dt, now, mood, theme, wx, precip_kind, when, day, message)
        return img
    else:
        draw_bear(img, f, mood, theme, raining=bool(wx and wx["now_kind"]), buddy=buddy,
                  hurry=hurry, lazy=lazy, hot=hot)
        if mood != "sleep":
            draw_speech(img, f, morning_page(dt, now, BUBBLE[2] - 4), lazy == "lounge")
    if weekend:
        pass
    elif buddy:
        draw_chairo(img, f, umbrella=rainy, wave=visit, sleep=mood == "sleep")
    elif mood != "sleep":
        if theme == "halloween":
            draw_sprite(img, CHAIRO_X, 23, PUMPKIN if (f // 3) % 3 else PUMPKIN_FLICKER)
        if theme == "easter":
            draw_sprite(img, CHAIRO_X + 2, 23, CROSS)
            glints = [(-1, 1), (5, 1), (-1, 5), (5, 5)]
            draw_dots(img, CHAIRO_X + 2, 23, [glints[(f // 5) % 4]], "s")
        if christmassy:
            star, swap = ("Y", "rUb") if (f // 5) % 2 else ("s", "bUr")
            tree = [row.replace("Y", star).translate(str.maketrans("rUb", swap)) for row in TREE]
            draw_sprite(img, CHAIRO_X, 21, tree)
    if theme in ("valentine", "anniversary") and mood != "sleep":
        draw_heart_fireworks(img, f)
    if mood != "sleep":
        if theme in ("spooky", "halloween") and not buddy and not weekend:
            bx = CHAIRO_X + round(2 * math.sin(f / 6))
            by = 20 + round((1 if theme == "halloween" else 2)
                                                               * math.cos(f / 6))
            draw_sprite(img, bx, by, BAT[(f // 2) % 2])
    if theme == "july4" and mood != "sleep":
        draw_fireworks(img, f)
    return img


# Night sky: (x, y, color, phase). They sit in the empty sky above the bear and twinkle
# out of step with each other; the two "big" ones sometimes sparkle into a little cross.
STARS = [(97, 2, (255, 240, 190), 0.0), (104, 7, (220, 210, 255), 1.3), (111, 2, (255, 255, 255), 2.1),
         (118, 8, (255, 225, 235), 0.7), (125, 3, (255, 240, 190), 2.8), (100, 13, (220, 210, 255), 1.9),
         (123, 13, (255, 255, 255), 3.4)]
BIG_STARS = {111, 125}


def draw_stars(img, now):
    px = img.load()
    for x, y, (r, g, b), phase in STARS:
        glow = 0.55 + 0.45 * math.sin(now * 1.3 + phase * 2.2)    # slow, gentle twinkle
        px[x, y] = (int(r * glow), int(g * glow), int(b * glow))
        if x in BIG_STARS:                                           # little 4-point stars
            arm = 0.25 + 0.4 * max(0, glow - 0.5)                    # arms flare as it brightens
            dim = (int(r * arm), int(g * arm), int(b * arm))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                if 0 <= y + dy < HEIGHT and 0 <= x + dx < WIDTH:
                    px[x + dx, y + dy] = dim


# ─── Weekend room ────────────────────────────────────────────────────────────
# On weekends the whole board becomes one cozy pink room. The weather lives in the
# window (with the temperature on a little tag below it), the date, high/low and
# clock sit on a butter-yellow framed board on the wall, and the bears and their
# little yellow chick friend Kiiroitori do different things through the day:
# pancakes and coffee in the morning, lounging to records in the afternoon, a movie
# in the evening, and tucked in asleep at night.
COUCH = [           # pink couch, 32 wide; drawn behind the bears
    "...aaaaaaaaaaaaaaaaaaaaaaaaaa...",
    "..aAAAAAAAAAAAAAAAAAAAAAAAAAAa..",
    "..aAAAAAAAAAAAAAAAAAAAAAAAAAAa..",
    "..aAAAAAAAAAAAAAAAAAAAAAAAAAAa..",
    "aaaAAAAAAAAAAAAAAAAAAAAAAAAAAaaa",
    "aAAaAAAAAAAAAAAAAAAAAAAAAAAAaAAa",
    "aAAaAAAAAAAAAAAAAAAAAAAAAAAAaAAa",
    "aAAajjjjjjjjjjjjjjjjjjjjjjjjaAAa",
    "aAAajJJJJJyJJJJJJJJJJJyJJJJjaAAa",
    "aAAajJJJJyyyJJJJJJJJJyyyJJJjaAAa",
    "aAAaajjjjjyjjjjjjjjjjjyjjjjaaAAa",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "..yy......................yy....",
]
COUCH_X, COUCH_Y = 21, 16
BLANKET = [         # night: a butter-yellow and white checked blanket over both bears
    "yyssyyssyyssyyssyyssyyssyy",
    "ssyyssyyssyyssyyssyyssyyss",
    "yyssyyssyyssyyssyyssyyssyy",
    "ssyyssyyssyyssyyssyyssyyss",
    "yyssyyssyyssyyssyyssyyssyy",
]
TV = [              # evening: a little TV on a stand
    "..l.....l..",
    "...l...l...",
    "....l.l....",
    "LLLLLLLLLLL",
    "L.........L",
    "L.........L",
    "L.........L",
    "L.........L",
    "L.........L",
    "L.........L",
    "LLLLLLLLLLL",
    "..f.....f..",
    ".fffffffff.",
    ".f.......f.",
    ".f.......f.",
]
PANCAKES = [        # morning: a pancake stack with butter and syrup on a little table
    "...yy....",
    ".PPPPPP..",
    "PPPPPPPP.",
    "SPSSPPSP.",
    "PPPPPPPP.",
    ".PPPPPP..",
    "fffffffff",
    "f.......f",
    "f.......f",
    "f.......f",
    "f.......f",
    "f.......f",
]
LAMP = [            # afternoon: a floor lamp with a pink-and-yellow shade
    "..hhhhh..",
    ".hhhhhhh.",
    "hyhhyhhyh",
    "....l....",
    "....l....",
    "....l....",
    "....l....",
    "....l....",
    "....l....",
    "....l....",
    "....l....",
    "....l....",
    "...lll...",
]
BOOK = ["hyy.yyh", "hyyyyyh"]                     # Chairo's open book
POPCORN = ["sYs.s", ".sssY", "hshsh", "hshsh", ".hsh."]
KIIROITORI = [      # the little yellow chick friend
    "..YYYY..",
    ".YYYYYY.",
    "YY.YY.YY",
    "YYYooYYY",
    "YYYYYYYY",
    ".YYYYYY.",
    "..o..o..",
]
KIIROI_ASLEEP = [   # tucked into a teacup at night
    "..YYYY..",
    ".YYYYYY.",
    "YY.YY.YY",
    "YYYooYYY",
    "hhhhhhhh",
    ".hhhhhh.",
]
RECORD_PLAYER = [
    "..xxxxx..l",
    ".xxxYxxx.l",
    "..xxxxx.l.",
    "hhhhhhhhhh",
    "hyhhhhhhyh",
    "hhhhhhhhhh",
]
NOTE = ["..h", "..h", "hhh", "hh."]
BALLOON = [         # a heart balloon on a wiggly string
    ".hh.hh.",
    "hhhhhhh",
    "hshhhhh",
    ".hhhhh.",
    "..hhh..",
    "...h...",
    "...l...",
    "....l..",
    "...l...",
    "....l..",
]
BOARD = (65, 1, 41, 28)       # the framed info board: x, y, width, height
WALL_HEART = [(0, 0), (2, 0), (0, 1), (1, 1), (2, 1), (1, 2)]


def _fill(px, x0, y0, w, h, color):
    for yy in range(y0, y0 + h):
        for xx in range(x0, x0 + w):
            px[xx, yy] = color


def draw_wall_and_floor(img, theme, dark):
    px = img.load()
    k = 0.55 if dark else 1.0
    wall = tuple(int(c * k) for c in (42, 14, 30))
    pink_h = tuple(int(c * k) for c in (115, 38, 75))
    yellow_h = tuple(int(c * k) for c in (115, 95, 35))
    _fill(px, 0, 0, WIDTH, HEIGHT, wall)               # one wall across the whole board
    for row, hy in enumerate(range(4, 27, 8)):         # tiny pink and butter hearts wallpaper
        for col, hx in enumerate(range(3 + 4 * (row % 2), WIDTH, 8)):
            color = pink_h if (row + col) % 2 else yellow_h
            for dx, dy in WALL_HEART:
                if hx + dx < WIDTH:
                    px[hx + dx, hy + dy] = color
    for xx in range(WIDTH):                            # pale floor with a fluffy pink rug
        px[xx, 29] = (205, 140, 150)
        for yy in (30, 31):
            rug = 16 <= xx <= 56
            px[xx, yy] = ((255, 165, 205) if (xx + yy) % 4 else (255, 230, 150)) if rug else (150, 95, 110)
    if theme in ("xmas", "christmas"):
        flags = [(235, 60, 60), (240, 240, 240), (60, 200, 90)]
    elif theme in ("spooky", "halloween"):
        flags = [(255, 140, 30), (240, 240, 240), (255, 205, 90)]
    else:
        flags = [(255, 150, 190), (255, 225, 120), (255, 240, 225)]
    k = 0
    for bx in list(range(19, 63, 5)) + list(range(108, 126, 5)):   # flag garland (skips the board)
        sag = 1 if (30 <= bx <= 50) or (110 <= bx <= 120) else 0
        for dx, dy in ((0, 0), (1, 0), (2, 0), (1, 1)):
            px[bx + dx, 1 + sag + dy] = flags[k % 3]
        k += 1


def draw_window(img, f, wx, night):
    """The window shows the real weather outside, using the same little weather icons
    as the weekday board, on a softly tinted sky, with pink curtains."""
    px = img.load()
    x0, y0, w, h = 2, 2, 15, 11
    icon = wx["icon"] if wx else ("moon" if night else "sun")
    gray = icon not in ("sun", "moon")
    sky = (8, 10, 32) if night else ((30, 32, 48) if gray else (28, 50, 95))
    for yy in range(y0, y0 + h):
        for xx in range(x0, x0 + w):
            edge = xx in (x0, x0 + w - 1) or yy in (y0, y0 + h - 1)
            px[xx, yy] = (255, 225, 140) if edge else sky
    draw_weather_icon(img, x0 + 3, y0 + 2, icon, f)
    for yy in range(y0 - 1, y0 + h + 1):                                       # pink curtains
        for cx in (x0 - 1, x0 + w):
            if yy % 3 or yy < y0:
                px[cx, yy] = (255, 150, 190)


def draw_temp_tag(img, wx):
    """The current temperature on a little tag hanging under the window."""
    px = img.load()
    x0, y0, w, h = 0, 16, 19, 9
    for yy in range(y0, y0 + h):
        for xx in range(x0, x0 + w):
            edge = xx in (x0, x0 + w - 1) or yy in (y0, y0 + h - 1)
            px[xx, yy] = (255, 150, 190) if edge else (14, 4, 10)
    draw_dots(img, 9, 13, [(0, 1), (0, 2)], "l")                                # string
    text = f"{wx['temp']}°" if wx else "--°"
    draw_text(img, x0 + (w - text_width(text)) // 2, y0 + 1, text, CREAM if wx else DIM)


def draw_info_board(img, dt, now, wx, precip_kind, when, day, message):
    px = img.load()
    bx, by, bw, bh = BOARD
    for yy in range(by, by + bh):
        for xx in range(bx, bx + bw):
            frame = xx in (bx, bx + bw - 1) or yy in (by, by + bh - 1)
            px[xx, yy] = (255, 215, 120) if frame else (14, 4, 10)
    for cx, cy in ((bx, by), (bx + bw - 1, by), (bx, by + bh - 1), (bx + bw - 1, by + bh - 1)):
        px[cx, cy] = (255, 150, 190)                                            # pink corners
    ix, iw = bx + 1, bw - 2
    scroll_s = (text_width(message) + iw) / SCROLL_PX_PER_S if message else 0
    if message and now % 60 < scroll_s:                 # special days: the message scrolls on the board
        offset = int((now % 60) * SCROLL_PX_PER_S)
        draw_text(img, ix + iw - offset, by + 2, message, THEME_COLORS.get(day, PINK), clip=(ix, ix + iw))
    elif morning_page(dt, now, iw - 2) is not None:   # the morning message, a few words at a time
        line = morning_page(dt, now, iw - 2)
        draw_tiny(img, ix + (iw - tiny_width(line)) // 2, by + 3, line, CREAM)
    else:
        date = f"{dt:%a} {dt.day}"
        draw_text(img, ix + (iw - text_width(date)) // 2, by + 2, date, PINK)
    if precip_kind:
        snowy = precip_kind == "snow"
        w = 7 + text_width(when)
        x = ix + (iw - w) // 2
        draw_sprite(img, x, by + 11, SNOWFLAKE_SMALL if snowy else UMBRELLA_SMALL)
        draw_text(img, x + 7, by + 10, when, ICY if snowy else BLUE)
    elif wx:
        draw_hilo(img, ix + (iw - text_width(f"{wx['hi']}°/{wx['lo']}°")) // 2, by + 10, wx["hi"], wx["lo"],
                  colors=(CREAM, (255, 150, 190), (255, 215, 120)))
    draw_clock(img, ix + (iw - 27) // 2, by + 18, dt, now)


def draw_balloons(img, f):
    """A pink and a butter-yellow heart balloon bobbing in the corner."""
    for x, y0, phase, recolor in ((109, 3, 0.0, None), (117, 6, 2.0, "y")):
        rows = BALLOON if not recolor else [r.replace("h", recolor) for r in BALLOON]
        draw_sprite(img, x, y0 + round(1.2 * math.sin(f / 9 + phase)), rows)


def draw_room(img, f, dt, now, mood, theme, wx, precip_kind, when, day, message):
    px = img.load()
    asleep = mood == "sleep"
    hour = dt.hour
    night = asleep or hour >= 18 or hour < 7
    draw_wall_and_floor(img, theme, dark=asleep or hour >= 22 or hour < 7)
    draw_window(img, f, wx, night)
    draw_temp_tag(img, wx)
    draw_info_board(img, dt, now, wx, precip_kind, when, day, message)
    draw_balloons(img, f)
    draw_sprite(img, COUCH_X, COUCH_Y, COUCH)
    kx, ky = COUCH_X + 3, 5                        # Korilakkuma on the couch (feet on the cushion)
    cx, cy = COUCH_X + 21, 12                      # Chairo beside her
    hop = 1 if f % 25 < 3 else 0                   # Kiiroitori's occasional little hop
    if asleep:
        draw_bear(img, f, "sleep", theme, x=kx, y=ky)
        draw_chairo(img, f, sleep=True, x=cx, y=cy)
        draw_sprite(img, COUCH_X + 3, 20, BLANKET)
        draw_sprite(img, 108, 23, RECORD_PLAYER)
        draw_sprite(img, 119, 23, KIIROI_ASLEEP)
        draw_dots(img, 119, 23, [(2, 2), (5, 2)], "Y")     # eyes closed
        t = (f // 6) % 6                           # a tiny z drifting up from the chick
        draw_dots(img, 126, 21 - t, [(0, 0)], "z")
    elif hour < 12:                                # breakfast: coffee for her, pancakes on the table
        draw_bear(img, f, "idle", theme, lazy="coffee", x=kx, y=ky)
        draw_chairo(img, f, x=cx, y=cy)
        draw_sprite(img, 54, 17, PANCAKES)
        wisp = (f // 3) % 2
        draw_dots(img, 54, 17, [(3 + (wisp + k) % 2, -1 - k) for k in range(3)], "g")
        draw_sprite(img, 108, 23, RECORD_PLAYER)
        draw_sprite(img, 119 + (f // 50) % 2, 22 - hop, KIIROITORI)       # pottering about
    elif hour < 18:                                # lazy afternoon: lounging, reading, records on
        draw_bear(img, f, "idle", theme, lazy="lounge", x=kx, y=ky)
        draw_chairo(img, f, x=cx, y=cy)
        draw_sprite(img, cx + 1, cy + 8, BOOK)
        draw_sprite(img, 54, 16, LAMP)
        spin = ["..xxxxx..l", ".xxYxxxx.l", "..xxxxx.l."] if (f // 3) % 2 else RECORD_PLAYER[:3]
        draw_sprite(img, 108, 23, spin + RECORD_PLAYER[3:])
        for k in range(2):                         # music notes floating up
            t = (f // 2 + k * 9) % 18
            draw_sprite(img, 109 + k * 4 + (t // 4) % 2, 20 - t, NOTE if k else
                        [r.replace("h", "y") for r in NOTE])
        draw_sprite(img, 120, 22 - (1 if (f // 8) % 2 else 0), KIIROITORI)  # bopping to the music
    else:                                          # movie night
        draw_bear(img, f, "idle", theme, x=kx, y=ky)
        draw_chairo(img, f, x=cx, y=cy)
        draw_sprite(img, 54, 14, TV)
        glow = [(255, 150, 190), (130, 190, 255), (255, 220, 140), (255, 190, 150)]
        scene = glow[(f // 15) % len(glow)]
        glint = 55 + (f // 3) % 9                  # a soft highlight drifting across the screen
        for yy in range(18, 24):
            for xx in range(55, 64):
                k = 0.9 if abs(xx - glint) < 2 else 0.55
                px[xx, yy] = tuple(int(c * k) for c in scene)
        draw_sprite(img, 57, 10, KIIROITORI)              # Kiiroitori perched on the TV
        draw_sprite(img, COUCH_X + 15, 24, POPCORN)
        draw_sprite(img, 108, 23, RECORD_PLAYER)
    if theme in ("valentine", "anniversary") and not asleep:
        for k in range(3):                         # little hearts floating up from the couch
            t = (f // 3 + k * 7) % 14
            draw_sprite(img, COUCH_X + 8 + k * 7, 15 - t, [".h.h.", "hhhhh", ".hhh.", "..h.."]
                        if t < 10 else [".h.h", "..h."])


def draw_chairo(img, f, umbrella=False, wave=False, sleep=False, x=CHAIRO_X, y=CHAIRO_Y):
    draw_sprite(img, x, y, CHAIRO)
    if sleep:
        draw_dots(img, x, y, [(2, 3), (3, 3), (6, 3), (7, 3)], None)   # closed eyes
        return
    if f % 40 in (10, 11, 12):                                       # blink (out of sync with the bear)
        draw_dots(img, x, y, [(2, 3), (7, 3)], "C")
    if umbrella:
        dy, rows = CHAIRO_UMBRELLA
        draw_sprite(img, x, y + dy, rows)
    elif wave:
        if (f // 5) % 2:
            draw_dots(img, x, y, [(-1, 6), (-1, 7), (0, 8)], "C")     # paw up
        if (f // 5) % 2 == 0:
            draw_sprite(img, x + 5, y - 5, HEART)                    # pulsing heart


def draw_weather_icon(img, x, y, kind, f):
    if kind == "sun":
        draw_sprite(img, x, y, SUN_A if (f // 8) % 2 == 0 else SUN_B)      # rays pulse
    elif kind == "moon":
        draw_sprite(img, x, y, MOON)
        stars = [(6, 1), (5, 5), (7, 3)]
        draw_dots(img, x, y, [stars[(f // 6) % 3]], "s")                  # twinkle
    elif kind == "cloud":
        draw_sprite(img, x + (f // 12) % 2, y + 2, CLOUD)                  # drift
    elif kind == "cloud_night":                  # cloudy night: the moon peeks out behind the cloud
        draw_sprite(img, x + 4, y, MOON_PEEK)
        draw_sprite(img, x + (f // 12) % 2, y + 3, CLOUD)
        if (f // 8) % 3 == 0:
            draw_dots(img, x, y, [(1, 1)], "s")                              # a lone twinkle
    else:
        draw_sprite(img, x, y, CLOUD)
        if kind == "rain":
            for col, off in [(1, 0), (4, 2), (7, 1)]:
                draw_dots(img, x, y, [(col, 4 + (f // 2 + off) % 4)], "d")
        elif kind == "snow":
            for col, off in [(1, 0), (4, 3), (7, 1)]:
                t = (f // 4 + off) % 4
                draw_dots(img, x, y, [(col + (t % 2), 4 + t)], "s")
        elif kind == "storm" and (f // 3) % 4 != 0:
            draw_sprite(img, x, y + 4, BOLT)


def draw_particles(img, x, y, f, kind):
    """Things drifting down around the bear: snow, confetti, petals, leaves..."""
    colors = PARTICLES[kind]
    sway = kind in ("petals", "leaves")
    for k, (col, speed, off) in enumerate([(-3, 2, 0), (4, 3, 7), (11, 2, 13), (18, 3, 4)]):
        t = (f // speed + off) % 24
        dx = ((t // 3) % 3 - 1) if sway else (t // 3) % 2
        draw_dots(img, x, y, [(col + dx, t - 6)], colors[k])


def draw_fireworks(img, f):
    """Little bursts in the sky above the bear."""
    for k, (cx, cy, ch) in enumerate([(104, 4, "R"), (118, 3, "s"), (111, 7, "b")]):
        t = (f + k * 7) % 21
        if t < 12:
            r = min(t // 2, 3)
            pts = [(r, 0), (-r, 0), (0, r), (0, -r), (r, r), (-r, -r), (r, -r), (-r, r)]
            draw_dots(img, cx, cy, pts if r else [(0, 0)], ch)


# Fixed, evenly spread columns for the full-board rain/snow: (x, phase)
BACKDROP = [(i * 7 + (i * 5) % 4, (i * 13) % 37) for i in range(18)]
BACKDROP_COLORS = {"rain": (45, 75, 140), "storm": (45, 75, 140), "snow": (150, 150, 175)}


def draw_weather_backdrop(img, f, kind):
    """Faint rain streaks or drifting snowflakes across both panels."""
    px = img.load()
    color = BACKDROP_COLORS.get(kind)
    if not color:
        return
    for x, phase in BACKDROP:
        if kind == "snow":
            y = (f // 2 + phase) % 36 - 2                       # slow
            pts = [(x + round(math.sin((f + phase) / 8)), y)]  # gentle sway
        else:
            y = (f * 2 + phase) % 40 - 4                        # fast
            dx = y // 10                                        # slight slant
            pts = [(x + dx, y), (x + dx, y - 1)]                # short streak
        for qx, qy in pts:
            if 0 <= qx < WIDTH and 0 <= qy < HEIGHT:
                px[qx, qy] = color


def draw_heart_fireworks(img, f):
    """Heart-shaped bursts in the sky above the bear (Valentine's Day, your anniversary)."""
    for k, (cx, cy, ch) in enumerate([(103, 4, "h"), (117, 3, "r"), (110, 6, "c")]):
        t = (f + k * 7) % 21
        if t < 3:                                                 # spark rising
            draw_dots(img, cx, cy, [(0, 3 - t)], ch)
        elif t < 14:
            size = 0 if t < 5 else 1 if t < 8 else 2
            if t >= 12 and f % 2:                                 # fading sparkle
                continue
            rows = [r.replace("h", ch) for r in HEART_BURSTS[size]]
            draw_sprite(img, cx - len(rows[0]) // 2, cy - len(rows) // 2, rows)


def draw_lights(img, f):
    """December: a string of twinkling Christmas lights under 'MANHATTAN' and the temperature."""
    colors = "RUbMhy"
    for i, bx in enumerate(range(2, 108, 5)):
        if (f // 5 + i) % 5:                                      # each bulb blinks now and then
            draw_dots(img, bx, 9 + (i % 2), [(0, 0)], colors[i % len(colors)])


def draw_bear(img, f, mood, theme, raining=False, buddy=False, hurry=False,
              lazy=None, hot=False, x=BEAR_X, y=BEAR_Y):
    ghost = theme == "halloween" and mood != "sleep"
    christmassy = theme in ("xmas", "christmas")
    hat = None
    if not ghost and mood != "sleep":
        # Hat: special days first, then the season, then the weather
        hat = (PARTY_HAT if theme in ("birthday", "newyear") else
               BUNNY_EARS if theme == "easter" else
               SANTA_HAT if christmassy else
               WITCH_HAT if theme == "spooky" else
               BEANIE if mood == "snow" else None)
    yawning = lazy and (f // 10) % YAWN_EVERY_S == 0 and f % 10 < 9   # ~1 s yawn
    falling = ("snow" if mood == "snow" or christmassy else
               "confetti" if theme in ("birthday", "newyear") else
               "hearts" if theme in ("valentine", "anniversary") else
               "clovers" if theme == "stpatricks" else
               "petals" if theme in ("spring", "easter") else
               "leaves" if theme in ("fall", "thanksgiving") else None)

    # Lazy weekend afternoon: lying on its belly (only when not dressed up)
    if lazy == "lounge" and not hat and not ghost and not christmassy:
        ly = y + 8
        draw_sprite(img, x, ly, LOUNGE)
        if yawning:
            draw_dots(img, x, ly, [(3, 4), (10, 4)], "W")
            draw_dots(img, x, ly, [(2, 5), (3, 5), (4, 5), (9, 5), (10, 5), (11, 5)], None)  # eyes shut
            draw_dots(img, x, ly, [(5, 6), (6, 6), (7, 6), (8, 6), (6, 7), (7, 7)], None)    # big yawn
            draw_dots(img, x, ly, [(6, 7), (7, 7)], "c")                                     # tongue
        if mood == "cold":
            draw_dots(img, x, ly, [(c, 7) for c in range(13, 19)], "b")        # blanket stripe
        if falling:
            draw_particles(img, x, y, f, falling)
        return

    if hurry:
        y -= f % 2                                              # quick nervous hops
    elif mood == "idle" and not lazy and (f // 10) % 2:
        y -= 1                                                  # gentle breathing bob
    draw_sprite(img, x, y, GHOST if ghost else BEAR)

    if mood == "sleep":
        draw_dots(img, x, y, EYES, "W")
        draw_dots(img, x, y, [(3, 7), (4, 7), (5, 7), (6, 7), (11, 7), (12, 7), (13, 7), (14, 7)], None)  # closed eyes
        if christmassy:
            draw_sprite(img, x, y + SANTA_SUIT[0], SANTA_SUIT[1])
        dy, rows = SANTA_HAT if christmassy else NIGHTCAP
        draw_sprite(img, x, y + dy, rows)
        for k in range(2):                                      # z's drift up
            t = (f + k * 20) % 40
            if t < 32:
                draw_sprite(img, x + 15 + (t // 8) % 2, y - 5 - t // 4, ZZZ)
        if christmassy:
            draw_particles(img, x, y, f, "snow")
        return

    # Body layer
    if christmassy:
        draw_sprite(img, x, y + SANTA_SUIT[0], SANTA_SUIT[1])
    elif mood in ("cold", "snow") and not ghost:
        draw_sprite(img, x, y + SCARF[0], SCARF[1])

    # Face
    if not ghost:
        if yawning:
            draw_dots(img, x, y, EYES, "W")
            draw_dots(img, x, y, [(3, 7), (4, 7), (5, 7), (6, 7), (11, 7), (12, 7), (13, 7), (14, 7)], None)  # eyes shut
            draw_dots(img, x, y, [(7, 9), (8, 9), (9, 9), (10, 9), (8, 10), (9, 10)], None)  # big yawn
            draw_dots(img, x, y, [(8, 10), (9, 10)], "c")                           # tongue
        elif lazy:
            draw_dots(img, x, y, [(4, 6), (5, 6), (12, 6), (13, 6)], "W")   # heavy, sleepy eyelids
        elif f % 40 in (30, 31, 32):                            # blink
            draw_dots(img, x, y, EYES, "W")
        if hot and not yawning:
            draw_sprite(img, x, y + SUNGLASSES[0], SUNGLASSES[1])

    if mood == "umbrella":   # held above any hat
        lift = min(0, (hat[0] if hat else 0) + 1)
        canopy = UMBRELLA[1][:5]
        draw_sprite(img, x, y + UMBRELLA[0] + lift, canopy)
        draw_dots(img, x, y, [(px_, py_) for py_ in range(-2 + lift, 1) for px_ in (8, 9)], "l")
        if raining:
            for col, off in [(-2, 0), (19, 5)]:
                t = (f // 2 + off) % 10
                draw_dots(img, x, y, [(col, t * 2 - 4)], "d")
    if hat:
        draw_sprite(img, x, y + hat[0], hat[1])

    # Little extras
    if hurry:
        draw_sprite(img, x + 17, y + 2, SWEAT)                  # sweat drop
        if not ghost:                                           # running feet
            lifted = [(c, 17) for c in range(3, 7)] if (f // 2) % 2 else \
                     [(c, 17) for c in range(11, 15)]
            draw_dots(img, x, y, lifted, None)
    elif lazy:
        draw_sprite(img, x + 14, y + 12, MUG)                   # hot coffee
        wisp = (f // 3) % 2                                     # curly steam
        draw_dots(img, x, y, [(15 + (wisp + k) % 2, 11 - k) for k in range(5)], "g")
        if yawning:
            draw_dots(img, x, y, [(-1, 12), (-2, 11)], "W")     # stretchy arm
    elif mood == "idle" and not buddy and theme is None and f % 40 < 20:
        draw_sprite(img, x + 13, y - 3 - (f % 40) // 4, HEART)  # a heart floats up

    if falling:
        draw_particles(img, x, y, f, falling)


def draw_scroll(img, x_min, x_max, y, msg, color, now):
    """Scroll msg right-to-left within columns [x_min, x_max)."""
    span = text_width(msg) + (x_max - x_min)
    offset = int(now * SCROLL_PX_PER_S) % span
    draw_text(img, x_max - offset, y, msg, color, clip=(x_min, x_max))


def brightness_now():
    return BRIGHTNESS_NIGHT if is_night(time.time()) else BRIGHTNESS_DAY

# ─── Outputs ─────────────────────────────────────────────────────────────────
class MatrixOut:
    """Two chained 64x32 panels driven by hzeller/rpi-rgb-led-matrix (see WIRING / GPIO_SLOWDOWN)."""
    def __init__(self):
        from rgbmatrix import RGBMatrix, RGBMatrixOptions
        o = RGBMatrixOptions()
        o.rows, o.cols, o.chain_length = HEIGHT, PANEL_W, 2
        o.hardware_mapping = WIRING
        o.gpio_slowdown = GPIO_SLOWDOWN
        o.drop_privileges = False
        self.matrix = RGBMatrix(options=o)
        self.canvas = self.matrix.CreateFrameCanvas()

    def show(self, img):
        self.matrix.brightness = brightness_now()
        self.canvas.SetImage(img)
        self.canvas = self.matrix.SwapOnVSync(self.canvas)


class TerminalOut:
    """Truecolor preview: two LED rows per terminal line using half-block characters."""
    def show(self, img):
        px = img.load()
        lines = []
        for y in range(0, HEIGHT, 2):
            row = ""
            for x in range(WIDTH):
                t, b = px[x, y], px[x, y + 1]
                row += f"\x1b[38;2;{t[0]};{t[1]};{t[2]}m\x1b[48;2;{b[0]};{b[1]};{b[2]}m▀"
            lines.append(row + "\x1b[0m")
        sys.stdout.write("\x1b[H" + "\n".join(lines) + "\n")
        sys.stdout.flush()


def save_png(img, path, scale=8):
    """Render with a round-LED look so you can see what the panels will look like."""
    big = Image.new("RGB", (img.width * scale, img.height * scale), (12, 12, 12))
    d = ImageDraw.Draw(big)
    px = img.load()
    pad = scale // 6
    for y in range(img.height):
        for x in range(img.width):
            c = px[x, y] if px[x, y] != (0, 0, 0) else (28, 28, 28)
            d.ellipse([x * scale + pad, y * scale + pad,
                       (x + 1) * scale - pad, (y + 1) * scale - pad], fill=c)
    big.save(path)

# ─── Main loop ───────────────────────────────────────────────────────────────
DISPLAY_SHIFT = 0   # seconds; set by --pretend to preview another date/time with live trains
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terminal", action="store_true", help="preview in the terminal")
    ap.add_argument("--png", metavar="PATH", help="render one frame to an image and exit")
    ap.add_argument("--pretend", metavar='"YYYY-MM-DD HH:MM"',
                    help="show the board as if it were this date/time (trains stay live)")
    args = ap.parse_args()
    if args.pretend:
        global DISPLAY_SHIFT
        DISPLAY_SHIFT = datetime.strptime(args.pretend, "%Y-%m-%d %H:%M").timestamp() - time.time()

    state = State()
    state.refresh_due(force=True)

    if args.png:
        save_png(render(state.snapshot(), state.now()), args.png)
        print(f"wrote {args.png}")
        return

    out = TerminalOut() if args.terminal else MatrixOut()
    if args.terminal:
        sys.stdout.write("\x1b[2J")
    threading.Thread(target=state.run_forever, daemon=True).start()
    while True:
        out.show(render(state.snapshot(), state.now()))
        time.sleep(1 / FPS - time.time() % (1 / FPS))   # wake on the next frame boundary


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
