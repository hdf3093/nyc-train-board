"""Copy this to my_settings.py and fill in your own details. my_settings.py is listed in
.gitignore, so your station, location and dates stay private. Anything you leave out
keeps the default from train_board.py."""
MTA = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"

# Your station. Stop ids are in the MTA's GTFS stops.txt; add "N" or "S" for direction.
# trip_stop decides which service alerts matter (stations between you and your destination).
LINES = [
    dict(route="L", feed=MTA + "nyct%2Fgtfs-l", stop="L08N", walk_min=5,
         origins={"L29N"},
         trip_stop=lambda s: s.startswith("L") and int(s[1:3]) <= 8),
    # a second line, e.g. the M:
    # dict(route="M", feed=MTA + "nyct%2Fgtfs-bdfm", stop="M11N", walk_min=4,
    #      origins={"M01N"}, trip_stop=lambda s: not s.startswith("M") or int(s[1:3]) >= 11),
]
HEADER = "MANHATTAN"
LAT, LON = 40.7128, -74.0060           # where to get the weather for

BUDDY_TIMES = ["11:11"]                # minutes when Chairoikoguma drops by

PERSONAL_DAYS = {
    # "MM-DD": ("birthday", "HAPPY BIRTHDAY!"),
    # "MM-DD": ("anniversary", "HAPPY ANNIVERSARY!"),
}
COUNTDOWNS = [
    # ("a trip somewhere", "2027-05-01"),   # one-time date
    ("christmas", "12-25"),                  # every year
]
