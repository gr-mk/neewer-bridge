#!/usr/bin/env python3
"""Checks `neewer follow` without a light or a bridge.

Run: python3 tests/test_follow.py

Each test gets an empty settings folder and a made-up sundial state file. The
bridge is replaced by one that only records the frames it is given.
"""

import argparse
import contextlib
import importlib.machinery
import io
import math
import importlib.util
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("neewer_cli", os.path.join(HERE, "..", "cli", "neewer"))
spec = importlib.util.spec_from_loader("neewer_cli", loader)
cli = importlib.util.module_from_spec(spec)
loader.exec_module(cli)

MAC = "AA:BB:CC:DD:EE:FF"
DAY = {"sun_elevation": 30.0, "daylight": 1.0, "kelvin": 6500}
DUSK = {"sun_elevation": 0.4, "daylight": 0.25, "kelvin": 4130}
NIGHT = {"sun_elevation": -30.0, "daylight": 0.0, "kelvin": 3400}


class FakeBridge:
    sent = []
    broken = False

    def __init__(self, base):
        pass

    def send(self, frame):
        if FakeBridge.broken:
            raise cli.Failure("cannot reach the bridge")
        FakeBridge.sent.append(cli.to_hex(frame))


class Scaffold(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.saved = {name: getattr(cli, name) for name in (
            "LIGHT_FILE", "STATE_FILE", "FOLLOW_FILE", "FOLLOW_MEMORY_FILE", "SUNDIAL_STATE_FILE",
            "SUNDIAL_CONFIG_FILE", "TRACKING_DIR", "Bridge", "pause")}
        self.addCleanup(lambda: [setattr(cli, name, value) for name, value in self.saved.items()])
        for name, file in (("LIGHT_FILE", "light.json"), ("STATE_FILE", "state.json"),
                           ("FOLLOW_FILE", "follow.json"), ("FOLLOW_MEMORY_FILE", "follow-state.json"),
                           ("SUNDIAL_STATE_FILE", "sundial.json"),
                           # No sundial settings: no bedtime, so these tests pass at any hour.
                           ("SUNDIAL_CONFIG_FILE", "sundial.ini")):
            setattr(cli, name, os.path.join(self.folder.name, file))
        cli.Bridge = FakeBridge
        FakeBridge.sent, FakeBridge.broken = [], False
        # These tests are of the older way, where a setting made by hand is
        # held for a time. TrackingTest is of the switch.
        cli.TRACKING_DIR = os.path.join(self.folder.name, "tracking")
        cli.write_json(os.path.join(cli.TRACKING_DIR, "settings.json"), {"switch": False})
        cli.write_json(cli.LIGHT_FILE, {"mac": MAC})
        self.now = time.time()
        self.sunrise = False
        self.sun(DAY)
        # The light as it might be found: set by hand some hours ago.
        self.set_elsewhere({"on": True, "mode": "white", "kelvin": 4500, "brightness": 100, "tint": 0},
                           minutes_ago=300)

    def sun(self, target):
        cli.write_json(cli.SUNDIAL_STATE_FILE, {"target": target})
        os.utime(cli.SUNDIAL_STATE_FILE, (self.now, self.now))

    def set_elsewhere(self, state, minutes_ago=0):
        cli.write_json(cli.STATE_FILE, state)
        at = self.now - minutes_ago * 60
        os.utime(cli.STATE_FILE, (at, at))

    def state(self):
        return cli.read_json(cli.STATE_FILE, {})

    def apply(self):
        """One run, as the minute timer makes it. Returns the log line."""
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        return cli.follow_apply(light, now=self.now, sunrise_running=lambda: self.sunrise)

    def later(self, minutes):
        self.now += minutes * 60

    def white(self, kelvin, brightness):
        return cli.to_hex(cli.white_frame(cli.parse_mac(MAC), kelvin, brightness))


class FollowTest(Scaffold):
    # -- the curve

    def test_curve_through_the_day(self):
        settings = dict(cli.FOLLOW_DEFAULTS)
        self.assertEqual(cli.follow_target(DAY, settings), (6500, 100))
        self.assertEqual(cli.follow_target(DUSK, settings), (4100, 29))
        self.assertEqual(cli.follow_target(NIGHT, settings), (2700, 5))
        # Half way down through twilight the colour is half way to the night colour.
        self.assertEqual(cli.follow_target({"sun_elevation": -3, "daylight": 0.1, "kelvin": 3500}, settings),
                         (3100, 14))

    def test_curve_only_falls_towards_night(self):
        settings = dict(cli.FOLLOW_DEFAULTS)
        last = (9999, 101)
        for step in range(0, 81):
            elevation = 10 - step / 4.0
            sun = {"sun_elevation": elevation, "daylight": max(0.0, (elevation + 6) / 26.0),
                   "kelvin": 6500 - (6500 - 3400) * min(1.0, max(0.0, (10 - elevation) / 14.0))}
            now = cli.follow_target(sun, settings)
            self.assertLessEqual(now[0], last[0])
            self.assertLessEqual(now[1], last[1])
            last = now
        self.assertEqual(last, (2700, 5))

    def test_cap_floor_and_the_lights_range(self):
        cli.write_json(cli.FOLLOW_FILE, {"day_brightness": 60, "night_brightness": 0, "night_kelvin": 1000})
        settings = cli.follow_settings()
        self.assertEqual(cli.follow_target(DAY, settings), (6500, 60))
        self.assertEqual(cli.follow_target(NIGHT, settings), (2500, 1))  # never 0: that would be off
        self.assertEqual(cli.follow_target(dict(DAY, kelvin=9000), settings)[0], 7500)

    def test_bad_settings_fall_back_to_defaults(self):
        cli.write_json(cli.FOLLOW_FILE, {"day_brightness": "bright", "enabled": "yes", "hold_minutes": 30})
        settings = cli.follow_settings()
        self.assertEqual(settings["day_brightness"], 100)
        self.assertIs(settings["enabled"], True)
        self.assertEqual(settings["hold_minutes"], 30)

    # -- sending

    def test_sets_the_light_then_does_not_repeat(self):
        self.assertIn("white 6500K at 100%", self.apply())
        self.assertEqual(FakeBridge.sent, [self.white(6500, 100)])
        self.assertEqual(self.state()["kelvin"], 6500)
        for _ in range(5):
            self.later(1)
            self.sun(DAY)
            self.assertIsNone(self.apply())
        self.assertEqual(len(FakeBridge.sent), 1)

    def test_follows_as_the_sun_moves(self):
        self.apply()
        self.later(1)
        self.sun(DUSK)
        self.assertIn("white 4100K at 29%", self.apply())
        self.later(1)
        self.sun(NIGHT)
        self.assertIn("white 2700K at 5%", self.apply())
        self.assertEqual(FakeBridge.sent, [self.white(6500, 100), self.white(4100, 29), self.white(2700, 5)])
        self.assertIs(self.state()["on"], True)

    def test_only_white_frames_and_never_power(self):
        for sun in (DAY, DUSK, NIGHT, DAY):
            self.sun(sun)
            self.apply()
            self.later(1)
        for frame in FakeBridge.sent:
            self.assertEqual(frame[2:4], "90")

    def test_nothing_is_sent_while_the_light_is_off(self):
        self.set_elsewhere({"on": False, "mode": "white", "kelvin": 4500, "brightness": 100}, minutes_ago=300)
        self.assertIn("the light is off", self.apply())
        self.later(1)
        self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])
        self.assertIs(self.state()["on"], False)

    def test_off_and_on_again_is_not_a_manual_change(self):
        self.apply()
        state = self.state()
        self.later(10)
        self.set_elsewhere(dict(state, on=False))
        self.assertIn("the light is off", self.apply())
        self.later(10)
        # Apple Home writes its own set of keys when it switches the light.
        self.set_elsewhere(dict(state, on=True, hue=0, saturation=100, curve="linear"))
        self.sun(NIGHT)
        self.assertIn("white 2700K at 5%", self.apply())

    def test_nothing_is_sent_during_a_sunrise(self):
        self.apply()
        self.sunrise = True
        self.later(1)
        self.set_elsewhere({"on": True, "mode": "channels", "brightness": 1, "warm": 3})
        self.sun(NIGHT)
        self.assertIn("a sunrise is running", self.apply())
        self.later(1)
        self.assertIsNone(self.apply())
        self.assertEqual(len(FakeBridge.sent), 1)
        # When it ends, what it left is held like any other setting.
        self.sunrise = False
        self.later(1)
        self.assertIn("set elsewhere", self.apply())
        self.assertEqual(len(FakeBridge.sent), 1)

    # -- manual changes

    def test_a_manual_change_is_held_then_followed_again(self):
        self.apply()
        self.later(5)
        self.set_elsewhere(dict(self.state(), kelvin=3200, brightness=40))
        self.sun(NIGHT)
        line = self.apply()
        self.assertIn("set elsewhere", line)
        self.later(60)
        self.sun(NIGHT)
        self.assertIsNone(self.apply())  # still held, and only one line about it
        self.assertEqual(len(FakeBridge.sent), 1)
        self.assertEqual(self.state()["brightness"], 40)
        self.later(61)
        self.sun(NIGHT)
        self.assertIn("white 2700K at 5%", self.apply())
        self.assertEqual(len(FakeBridge.sent), 2)

    def test_power_switching_does_not_restart_a_hold(self):
        self.apply()
        self.later(5)
        manual = dict(self.state(), kelvin=3200, brightness=40)
        self.set_elsewhere(manual)
        self.apply()
        self.later(100)
        self.set_elsewhere(dict(manual, on=False))
        self.apply()
        self.later(30)
        self.set_elsewhere(manual)
        self.sun(NIGHT)
        self.assertIn("white 2700K at 5%", self.apply())  # 130 minutes after the change

    def test_a_second_manual_change_restarts_the_hold(self):
        self.apply()
        self.later(5)
        self.set_elsewhere(dict(self.state(), brightness=40))
        self.apply()
        self.later(100)
        self.set_elsewhere(dict(self.state(), brightness=41))
        self.apply()
        self.later(100)
        self.sun(DAY)
        self.assertIsNone(self.apply())
        self.assertEqual(len(FakeBridge.sent), 1)

    def test_hold_minutes_setting(self):
        cli.write_json(cli.FOLLOW_FILE, {"hold_minutes": 10})
        self.apply()
        self.later(5)
        self.set_elsewhere(dict(self.state(), brightness=40))
        self.apply()
        self.later(11)
        self.sun(DAY)
        self.assertIn("white 6500K at 100%", self.apply())

    # -- off, on, and things going wrong

    def test_switched_off_in_settings(self):
        cli.write_json(cli.FOLLOW_FILE, {"enabled": False})
        self.assertIn("switched off", self.apply())
        self.later(1)
        self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])

    def test_an_outage_is_one_line_and_recovers(self):
        FakeBridge.broken = True
        self.assertIn("cannot follow the sun", self.apply())
        for _ in range(3):
            self.later(1)
            self.sun(DAY)
            self.assertIsNone(self.apply())
        self.assertEqual(self.state()["kelvin"], 4500)  # nothing recorded as sent
        FakeBridge.broken = False
        self.later(1)
        self.sun(DAY)
        self.assertIn("white 6500K at 100%", self.apply())

    def test_old_or_missing_sundial_state(self):
        self.later(30)  # sundial has not written for half an hour
        self.assertIn("sundial last ran", self.apply())
        self.later(1)
        self.assertIsNone(self.apply())
        os.remove(cli.SUNDIAL_STATE_FILE)
        self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])

    def test_follow_on_ends_a_hold(self):
        self.apply()
        self.later(5)
        self.set_elsewhere(dict(self.state(), brightness=40))
        self.assertIn("set elsewhere", self.apply())
        arguments = argparse.Namespace(bridge=None, mac=None, quiet=True, action="on")
        cli.cmd_follow(cli.Light(arguments), arguments)
        self.assertEqual(len(FakeBridge.sent), 2)
        self.assertIs(cli.read_json(cli.FOLLOW_FILE, {})["enabled"], True)


class BedtimeScaffold(Scaffold):
    """An evening with sundial saying bedtime 23:00 and wake 07:00, and a clock to move."""

    def setUp(self):
        super().setUp()
        with open(cli.SUNDIAL_CONFIG_FILE, "w") as handle:
            handle.write("[sleep]\nwake = 07:00\nbedtime = 23:00   # a comment\n")
        self.paused, self.during = 0.0, None
        cli.pause = self.pause
        self.midnight = time.mktime(time.localtime(self.now)[:3] + (0, 0, 0, 0, 0, -1))
        # An evening: the follower has had the light at the night level for hours.
        self.at(22, 0)
        self.set_elsewhere({"on": True, "mode": "white", "kelvin": 4500, "brightness": 100, "tint": 0},
                           minutes_ago=300)
        self.sun(NIGHT)
        self.assertIn("white 2700K at 5%", self.apply())
        FakeBridge.sent = []

    def pause(self, seconds):
        self.paused += seconds
        if self.during:
            self.during()

    def at(self, hour, minute, day=0):
        self.now = self.midnight + day * 86400 + hour * 3600 + minute * 60
        self.sun(self.sky if hasattr(self, "sky") else NIGHT)

    def sun(self, target):
        self.sky = target
        super().sun(target)

    def by_hand(self, **changes):
        cli.write_json(cli.STATE_FILE, dict(self.state(), **changes))
        os.utime(cli.STATE_FILE, (self.now, self.now))

    def power(self, on):
        return cli.to_hex(cli.power_frame(cli.parse_mac(MAC), on))

    def status(self):
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        return cli.bedtime_outlook(light, cli.follow_settings(), cli.read_json(cli.FOLLOW_MEMORY_FILE, {}), self.now)

class BedtimeTest(BedtimeScaffold):
    """The bedtime rule."""

    def test_times_come_from_sundial(self):
        self.assertEqual(cli.sleep_times(cli.follow_settings()), (23 * 60, 7 * 60))
        self.assertEqual(self.status(), "off at 23:00, on again at 06:30")

    def test_fade_and_off_at_bedtime(self):
        self.at(23, 0)
        line = self.apply()
        self.assertEqual(line, "bedtime: faded out and switched off; off for the night; on at 06:30")
        self.assertEqual(FakeBridge.sent, [self.white(2700, 4), self.white(2700, 3), self.white(2700, 2),
                                           self.white(2700, 1), self.power(False), self.white(2700, 5)])
        self.assertAlmostEqual(self.paused, 300.3, places=1)  # five minutes
        # The record keeps the setting from before the fade, so "on" brings it back.
        self.assertEqual((self.state()["on"], self.state()["brightness"]), (False, 5))
        self.assertEqual(self.status(), "off for the night; on at 06:30")
        FakeBridge.sent = []
        for minute in (1, 30, 59):
            self.at(23, minute)
            self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])

    def test_already_off_at_bedtime(self):
        self.by_hand(on=False)
        self.at(23, 0)
        self.assertEqual(self.apply(), "off by hand; left alone tonight")
        self.at(6, 30, day=1)
        self.apply()
        self.at(9, 0, day=1)
        self.apply()
        self.assertEqual(FakeBridge.sent, [])  # he switched it off; it stays off
        self.assertIs(self.state()["on"], False)

    def test_switched_back_on_in_the_night_is_left_alone(self):
        self.at(23, 0)
        self.apply()
        FakeBridge.sent = []
        self.at(1, 10, day=1)
        self.by_hand(on=True)
        self.assertEqual(self.apply(), "on by hand since 01:10; left alone tonight")
        self.assertEqual(self.status(), "on by hand since 01:10; left alone tonight")
        for hour in (2, 4, 6):  # well past the two-hour hold: still not switched off again
            self.at(hour, 0, day=1)
            self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])
        self.assertIs(self.state()["on"], True)
        # In the morning it follows again.
        self.sun(DUSK)
        self.at(6, 30, day=1)
        self.assertIn("white 4100K at 29%", self.apply())

    def test_set_brighter_in_the_night_is_left_alone(self):
        self.at(23, 0)
        self.apply()
        self.at(0, 30, day=1)
        self.by_hand(on=True, brightness=60)
        self.apply()
        FakeBridge.sent = []
        self.at(3, 0, day=1)
        self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])

    def test_switched_off_by_hand_stays_off_in_the_morning(self):
        self.at(23, 0)
        self.apply()
        self.at(1, 0, day=1)
        self.by_hand(on=True)
        self.apply()
        self.at(2, 0, day=1)
        self.by_hand(on=False)
        self.assertEqual(self.apply(), "off by hand; left alone tonight")
        FakeBridge.sent = []
        for hour, minute in ((6, 30), (7, 0), (12, 0)):
            self.at(hour, minute, day=1)
            self.apply()
        self.assertEqual(FakeBridge.sent, [])
        self.assertIs(self.state()["on"], False)

    def test_on_again_in_the_morning_and_following(self):
        self.at(23, 0)
        self.apply()
        FakeBridge.sent = []
        self.at(6, 29, day=1)
        self.assertIsNone(self.apply())
        self.sun({"sun_elevation": -5.0, "daylight": 0.04, "kelvin": 3400})
        self.at(6, 30, day=1)
        self.assertEqual(self.apply(), "morning: switched on at white 2800K at 9%")
        self.assertEqual(FakeBridge.sent, [self.power(True), self.white(2800, 9)])
        self.assertIs(self.state()["on"], True)
        self.sun(DUSK)
        self.at(6, 50, day=1)
        self.assertIn("white 4100K at 29%", self.apply())  # no hold: it rises with the dawn
        self.assertEqual(self.status(), "off at 23:00, on again at 06:30")

    def test_morning_on_can_be_switched_off(self):
        cli.write_json(cli.FOLLOW_FILE, {"morning_on": False})
        self.at(23, 0)
        self.assertEqual(self.apply(), "bedtime: faded out and switched off; off for the night")
        FakeBridge.sent = []
        self.at(6, 30, day=1)
        self.apply()
        self.at(8, 0, day=1)
        self.apply()
        self.assertEqual(FakeBridge.sent, [])

    def test_a_sunrise_in_the_morning_is_left_alone(self):
        self.at(23, 0)
        self.apply()
        FakeBridge.sent = []
        self.sunrise = True
        self.at(6, 30, day=1)
        self.apply()
        self.sunrise = False
        self.at(6, 40, day=1)
        self.apply()
        self.assertEqual(FakeBridge.sent, [])  # the sunrise's business now, not switched on twice

    def test_own_bedtime_and_wake(self):
        cli.write_json(cli.FOLLOW_FILE, {"bedtime": "21:30", "wake": "06:00", "morning_lead_minutes": 15,
                                         "fade_minutes": 1})
        self.at(21, 29, day=1)
        self.assertEqual(self.status(), "off at 21:30, on again at 05:45")
        self.apply()
        self.assertIs(self.state()["on"], True)
        self.at(21, 30, day=1)
        self.assertIn("on at 05:45", self.apply())
        self.assertAlmostEqual(self.paused, 60.3, places=1)
        FakeBridge.sent = []
        self.at(5, 44, day=2)
        self.assertIsNone(self.apply())
        self.at(5, 45, day=2)
        self.assertIn("morning: switched on", self.apply())

    def test_the_rule_can_be_switched_off(self):
        cli.write_json(cli.FOLLOW_FILE, {"bedtime_rule": False})
        self.assertEqual(self.status(), "the rule is switched off")
        self.at(23, 0)
        self.assertIsNone(self.apply())
        self.sun(DUSK)
        self.at(23, 5)
        self.assertIn("white 4100K at 29%", self.apply())  # still following, still on
        self.assertIs(self.state()["on"], True)

    def test_started_in_the_middle_of_the_night(self):
        os.remove(cli.FOLLOW_MEMORY_FILE)
        self.at(2, 45, day=1)
        self.assertEqual(self.status(), "on by hand; left alone tonight")
        self.assertEqual(self.apply(), "on by hand; left alone tonight")
        self.at(3, 0, day=1)
        self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])
        self.at(9, 0, day=1)
        self.apply()                       # day: following again
        self.assertEqual(self.status(), "off at 23:00, on again at 06:30")
        FakeBridge.sent = []
        self.at(23, 0, day=1)              # the next bedtime is the first it acts on
        self.assertIn("faded out and switched off", self.apply())

    def test_a_late_start_after_bedtime_leaves_the_night_alone(self):
        self.at(23, 40)                    # the Mac was asleep at 23:00
        self.assertEqual(self.apply(), "on by hand; left alone tonight")
        self.assertEqual(FakeBridge.sent, [])

    def test_a_setting_made_during_the_fade_stops_it(self):
        def meddle():
            if self.paused >= 120 and self.state()["brightness"] != 50:
                self.now += 120
                self.by_hand(brightness=50)
        self.during = meddle
        self.at(23, 0)
        line = self.apply()
        self.assertIn("on by hand since", line)
        self.assertNotIn(self.power(False), FakeBridge.sent)
        self.assertEqual((self.state()["on"], self.state()["brightness"]), (True, 50))
        FakeBridge.sent = []
        self.at(23, 30)
        self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])

    def test_a_held_setting_is_still_switched_off_at_bedtime(self):
        self.at(22, 30)
        self.by_hand(brightness=40, kelvin=3000)
        self.assertIn("set elsewhere", self.apply())
        self.at(23, 0)
        self.assertIn("faded out and switched off", self.apply())
        self.assertEqual((self.state()["on"], self.state()["brightness"], self.state()["kelvin"]), (False, 40, 3000))

    def test_no_bedtime_known(self):
        os.remove(cli.SUNDIAL_CONFIG_FILE)
        self.assertIn("no bedtime known", self.status())
        self.at(23, 0)
        self.assertIsNone(self.apply())
        self.assertIs(self.state()["on"], True)


class FreshStartTest(BedtimeScaffold):
    """A light switched on by day after being off goes straight to the sun's setting."""

    def outlook(self):
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        return cli.fresh_outlook(cli.follow_settings(), cli.read_json(cli.FOLLOW_MEMORY_FILE, {}),
                                 light.state.get("on") is True, self.now)

    def off_overnight(self):
        """He switches the light off by hand at 03:20, in last night's warm white."""
        self.at(23, 0)
        self.apply()                      # the follower fades it out
        self.at(1, 0, day=1)
        self.by_hand(on=True)             # he switches it back on
        self.apply()
        self.at(3, 20, day=1)
        self.by_hand(on=False)            # and off again, himself
        self.apply()
        for hour in (6, 7, 9):
            self.at(hour, 30, day=1)
            self.apply()
        FakeBridge.sent = []

    def test_switched_on_in_the_morning_goes_to_the_sun(self):
        self.off_overnight()
        self.assertIn("when the light is switched on", self.outlook())
        self.sun(DAY)
        self.at(10, 58, day=1)
        self.by_hand(on=True, brightness=40)  # on, and the brightness touched, before the follower's next look
        self.assertEqual(self.outlook(), "due now")
        self.assertEqual(self.apply(), "fresh start: switched on, so set to the sun's white 6500K at 100%")
        self.assertEqual(FakeBridge.sent, [self.white(6500, 100)])
        self.assertEqual((self.state()["kelvin"], self.state()["brightness"]), (6500, 100))
        self.assertEqual(self.outlook(), "made at 10:58")
        self.at(10, 59, day=1)
        self.assertIsNone(self.apply())       # followed, not held

    def test_after_the_fresh_start_the_hold_is_as_ever(self):
        self.off_overnight()
        self.sun(DAY)
        self.at(10, 58, day=1)
        self.by_hand(on=True)
        self.apply()
        self.at(12, 0, day=1)
        self.by_hand(brightness=50)
        self.assertIn("set elsewhere", self.apply())
        self.at(13, 0, day=1)
        self.assertIsNone(self.apply())
        self.assertEqual(self.state()["brightness"], 50)

    def test_switched_on_in_the_night_is_left_alone(self):
        self.at(23, 0)
        self.apply()
        FakeBridge.sent = []
        self.at(2, 0, day=1)
        self.by_hand(on=True, brightness=40)
        self.assertIn("left alone tonight", self.apply())
        self.assertEqual(self.outlook(), "due in the morning, if the light is on then")
        self.at(4, 0, day=1)
        self.apply()
        self.assertEqual(FakeBridge.sent, [])
        self.assertEqual(self.state()["brightness"], 40)
        self.assertIsNone(cli.fresh_start_look(self.now))  # nor from Apple Home at this hour
        # Still on when the day begins: the day begins afresh.
        self.sun(DUSK)
        self.at(6, 30, day=1)
        self.assertIn("fresh start", self.apply())
        self.assertEqual(self.state()["brightness"], 29)

    def test_a_light_never_switched_off_is_held_as_ever(self):
        self.sun(DAY)
        self.at(9, 0)
        self.apply()
        self.at(15, 0)
        self.by_hand(brightness=40)
        self.assertIn("set elsewhere", self.apply())
        self.at(16, 0)
        self.assertIsNone(self.apply())
        self.assertEqual(self.state()["brightness"], 40)
        self.at(17, 1)
        self.assertIn("white 6500K at 100%", self.apply())

    def test_off_and_on_by_day(self):
        self.sun(DAY)
        self.at(9, 0)
        self.apply()
        self.at(14, 0)
        self.by_hand(brightness=40)          # a setting by hand, held
        self.apply()
        self.at(14, 10)
        self.by_hand(on=False)
        self.apply()
        self.at(15, 0)
        self.by_hand(on=True)
        self.assertIn("fresh start", self.apply())  # not back to the held 40%
        self.assertEqual(self.state()["brightness"], 100)

    def test_it_can_be_switched_off(self):
        cli.write_json(cli.FOLLOW_FILE, {"fresh_start": False})
        self.off_overnight()
        self.assertEqual(self.outlook(), "switched off")
        self.sun(DAY)
        self.at(10, 58, day=1)
        self.by_hand(on=True, brightness=40)
        self.assertIn("set elsewhere", self.apply())  # as it was: the old colour, held
        self.assertEqual((self.state()["kelvin"], self.state()["brightness"]), (2700, 40))
        self.assertIsNone(cli.fresh_start_look(self.now))

    def test_apple_home_gets_the_suns_setting_as_it_switches_on(self):
        self.off_overnight()
        self.sun(DAY)
        self.at(10, 58, day=1)
        self.assertEqual(cli.fresh_start_look(self.now), (6500, 100))
        # The Home service sends that and records it as the follower's own.
        self.by_hand(on=True, kelvin=6500, brightness=100)
        cli.fresh_start_done(self.now)
        self.assertEqual(self.outlook(), "made at 10:58")
        self.at(10, 59, day=1)
        self.assertIn("already at white 6500K at 100%", self.apply())  # nothing to send, and no hold
        self.assertEqual(FakeBridge.sent, [])
        self.sun(DUSK)
        self.at(11, 0, day=1)
        self.assertIn("white 4100K at 29%", self.apply())


class TrackingTest(BedtimeScaffold):
    """The sun tracking switch: no timer; a setting by hand turns it off for everything."""

    def setUp(self):
        super().setUp()
        os.remove(os.path.join(cli.TRACKING_DIR, "settings.json"))  # the switch is the default
        self.sun(DAY)
        self.at(9, 0, day=1)
        self.apply()
        FakeBridge.sent = []

    def test_it_starts_on(self):
        self.assertTrue(cli.tracking()["on"])
        self.assertTrue(cli.tracking_settings()["switch"])

    def test_a_setting_by_hand_turns_it_off_with_no_timer(self):
        self.at(10, 0, day=1)
        self.by_hand(brightness=40, kelvin=3000)
        line = self.apply()
        self.assertIn("sun tracking is off since 10:00, when the light was set by hand", line)
        track = cli.tracking()
        self.assertEqual((track["on"], track["by"]), (False, "light"))
        for hour in (11, 14, 20):                      # long past any two hours
            self.at(hour, 0, day=1)
            self.assertIsNone(self.apply())
        self.assertEqual(FakeBridge.sent, [])
        self.assertEqual((self.state()["brightness"], self.state()["kelvin"]), (40, 3000))

    def test_switched_on_again_it_follows_at_once(self):
        self.at(10, 0, day=1)
        self.by_hand(brightness=40, kelvin=3000)
        self.apply()
        self.at(12, 0, day=1)
        cli.set_tracking(True, "home", now=self.now)
        self.assertIn("white 6500K at 100%", self.apply())   # the old setting by hand is taken over
        self.assertTrue(cli.tracking()["on"])
        self.at(12, 1, day=1)
        self.assertIsNone(self.apply())

    def test_switching_the_light_off_and_on_is_not_a_setting(self):
        self.at(10, 0, day=1)
        self.by_hand(on=False)
        self.apply()
        self.at(10, 30, day=1)
        self.by_hand(on=True)
        self.apply()
        self.assertTrue(cli.tracking()["on"])

    def test_switched_off_by_hand_everything_is_left_alone(self):
        cli.set_tracking(False, "home", now=self.now)
        self.sun(DUSK)
        self.at(16, 0, day=1)
        self.assertIn("sun tracking is off since 09:00, switched off from Apple Home", self.apply())
        self.assertEqual(FakeBridge.sent, [])

    def test_it_turns_itself_on_before_sunrise(self):
        self.at(20, 0, day=1)
        self.by_hand(brightness=40)
        self.apply()
        self.assertFalse(cli.tracking()["on"])
        self.at(6, 29, day=2)
        self.apply()
        self.assertFalse(cli.tracking()["on"])
        self.at(6, 30, day=2)                           # the morning time; this sundial has no place set
        self.apply()
        track = cli.tracking()
        self.assertEqual((track["on"], track["by"]), (True, "dawn"))
        self.assertIn("switched on before sunrise", cli.tracking_text())

    def test_civil_dawn_comes_first_when_it_is_earlier(self):
        with open(cli.SUNDIAL_CONFIG_FILE, "w") as handle:
            handle.write("[location]\nlatitude = 40.71\nlongitude = -74.01\n[sleep]\nwake = 11:00\nbedtime = 23:00\n")
        self.at(12, 0, day=1)
        dawn = cli.civil_dawn(self.now)
        self.assertAlmostEqual(cli.sun_elevation(dawn, 40.71, -74.01), -6, delta=0.3)
        self.assertLess(cli.sun_elevation(dawn - 60, 40.71, -74.01), -6)
        self.assertTrue(4 * 3600 < dawn - self.midnight - 86400 < 8 * 3600)  # somewhere between 04:00 and 08:00
        self.at(20, 0, day=1)
        cli.set_tracking(False, "home", now=self.now)
        self.now = dawn + 86400 - 600                    # ten minutes before the next civil dawn, near enough
        self.sun(NIGHT)
        self.apply()
        self.assertFalse(cli.tracking()["on"])
        self.now = cli.civil_dawn(self.now + 3600) + 30
        self.sun(NIGHT)
        self.apply()
        self.assertTrue(cli.tracking()["on"])            # hours before the 10:30 morning time

    def test_switched_off_after_dawn_it_stays_off_all_day(self):
        self.at(8, 0, day=1)
        cli.set_tracking(False, "home", now=self.now)
        for hour in (9, 15, 22):
            self.at(hour, 0, day=1)
            self.apply()
        self.assertFalse(cli.tracking()["on"])
        self.at(6, 30, day=2)
        self.apply()
        self.assertTrue(cli.tracking()["on"])

    def test_it_can_be_told_not_to_turn_itself_on(self):
        cli.write_json(os.path.join(cli.TRACKING_DIR, "settings.json"), {"on_before_sunrise": False})
        cli.set_tracking(False, "home", now=self.now)
        self.at(7, 0, day=2)
        self.apply()
        self.assertFalse(cli.tracking()["on"])

    def test_bedtime_comes_whatever_the_switch_says(self):
        self.at(20, 0, day=1)
        self.by_hand(brightness=40)
        self.apply()
        self.sun(NIGHT)
        self.at(23, 0, day=1)
        self.assertIn("faded out and switched off", self.apply())
        self.assertFalse(cli.tracking()["on"])
        self.sun(DUSK)
        self.at(6, 30, day=2)
        self.assertIn("morning: switched on", self.apply())  # and by then tracking is on again
        self.assertTrue(cli.tracking()["on"])

    def test_a_fresh_start_does_not_turn_tracking_on(self):
        self.at(10, 0, day=1)
        self.by_hand(brightness=40, kelvin=3000)
        self.apply()
        self.at(11, 0, day=1)
        self.by_hand(on=False)
        self.apply()
        self.at(12, 0, day=1)
        self.by_hand(on=True)
        self.assertIn("fresh start", self.apply())       # the light comes on at the sun's setting
        self.assertEqual((self.state()["kelvin"], self.state()["brightness"]), (6500, 100))
        self.assertFalse(cli.tracking()["on"])           # but he switched tracking off, and off it stays
        FakeBridge.sent = []
        self.sun(DUSK)
        self.at(13, 0, day=1)
        self.apply()
        self.assertEqual(FakeBridge.sent, [])

    def test_a_scene_with_the_switch_and_colours_ends_with_the_colours(self):
        cli.set_tracking(False, "light", "set from Apple Home", now=self.now)   # the scene's colour arrives
        self.assertFalse(cli.set_tracking(True, "home", now=self.now + 2)["on"])  # and its "on" a moment later
        cli.set_tracking(True, "home", now=self.now + 60)                        # a tap a minute later does count
        self.assertTrue(cli.tracking()["on"])
        cli.set_tracking(False, "light", now=self.now + 61)                      # the other order: "on", then the colour
        self.assertFalse(cli.tracking()["on"])
        self.assertTrue(cli.set_tracking(True, "cli", now=self.now + 62)["on"])  # the command is never refused

    def test_a_sunrise_does_not_turn_tracking_off(self):
        self.sunrise = True
        self.at(9, 30, day=1)
        self.apply()
        self.at(9, 59, day=1)
        self.apply()
        self.by_hand(mode="channels", brightness=100, warm=120, cold=56)  # the ramp's last step
        self.sunrise = False
        self.at(10, 0, day=1)
        self.assertIn("white 6500K at 100%", self.apply())   # taken over, not left alone
        self.assertTrue(cli.tracking()["on"])

    def test_the_command(self):
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        cli.cmd_tracking(light, argparse.Namespace(action="off"))
        self.assertEqual((cli.tracking()["on"], cli.tracking()["by"]), (False, "cli"))
        cli.cmd_tracking(light, argparse.Namespace(action="on"))
        self.assertTrue(cli.tracking()["on"])
        cli.cmd_tracking(light, argparse.Namespace(action="status"))

    def test_the_old_way_is_still_there(self):
        cli.write_json(os.path.join(cli.TRACKING_DIR, "settings.json"), {"switch": False})
        self.at(10, 0, day=1)
        self.by_hand(brightness=40)
        self.assertIn("following again at 12:00", self.apply())
        self.assertTrue(cli.tracking()["on"])
        self.assertIn("not in use", cli.tracking_text())


AS600B = {"model": "AS600B"}
EXP, LINEAR = "01", "04"


class AS600BScaffold(Scaffold):
    """The AS600B: white only, 2700-6500K, no channels frame, two curves."""

    def setUp(self):
        super().setUp()
        cli.write_json(cli.LIGHT_FILE, {"mac": MAC, "profile": AS600B})
        self.waits = []
        saved = cli.pause
        cli.pause = self.waits.append
        self.addCleanup(lambda: setattr(cli, "pause", saved))

    def on(self, curve, number, kelvin=5600):
        """The light as last sent: `number` on `curve`."""
        cli.write_json(cli.STATE_FILE, {"on": True, "mode": "white", "kelvin": kelvin, "brightness": number,
                                        "curve": curve, "tint": 0})
        FakeBridge.sent = []

    def run_command(self, *words):
        args = cli.build_parser().parse_args(["--quiet"] + list(words))
        args.function(cli.Light(args), args)

    def frame(self, kelvin, number, curve):
        return cli.to_hex(cli.white_frame(cli.parse_mac(MAC), kelvin, number, 0, curve))

    @staticmethod
    def frames_of(sent):
        """The white frames sent, as (number, curve), in order."""
        curves = {EXP: "exp", LINEAR: "linear"}
        return [(int(frame[20:22], 16), curves.get(frame[26:28], frame[26:28])) for frame in sent if frame[2:4] == "90"]


class AS600BTest(AS600BScaffold):
    def test_the_profile(self):
        profile = cli.light_profile()
        self.assertEqual((profile["model"], profile["kelvin_min"], profile["kelvin_max"]), ("AS600B", 2700, 6500))
        self.assertFalse(profile["channels"] or profile["colour"])
        hybrid = profile["hybrid"]
        self.assertEqual((hybrid["threshold"], hybrid["hysteresis"], hybrid["switch_pause"]), (20, (18, 22), 1.5))
        self.assertEqual(hybrid["exp_table"], [(1, 0.0001), (45, 0.05), (55, 0.2)])
        self.assertIn("exponential curve below 20% output, linear above", cli.describe_profile(profile))
        cli.write_json(cli.LIGHT_FILE, {"mac": MAC, "profile": dict(AS600B, hybrid={"exp_table": [[1, 0.5], [9, 0.1]]})})
        self.assertEqual(cli.light_profile()["hybrid"], {})   # a table that does not rise: one curve after all
        cli.write_json(cli.LIGHT_FILE, {"mac": MAC})       # no profile: the CB200C, as before
        self.assertEqual((cli.light_profile()["model"], cli.light_profile()["hybrid"]), ("CB200C", {}))

    def test_the_mapping_on_both_sides(self):
        profile = cli.light_profile()
        number = lambda curve, output: cli.curve_number(profile, curve, output)
        self.assertEqual(number("exp", 5), 45)           # measured: exp 45 = linear 5
        self.assertEqual(number("exp", 20), 55)          # measured: exp 55 = linear 20
        self.assertEqual(number("exp", 0.01), 1)         # the guessed bottom
        self.assertEqual(number("linear", 20), 20)
        self.assertEqual(number("linear", 63.4), 63)
        self.assertAlmostEqual(cli.curve_output(profile, "exp", 45), 0.05)
        self.assertAlmostEqual(cli.curve_output(profile, "linear", 30), 0.30)
        power = math.log(0.05 / 0.0001) / math.log(45)    # between exp 1 and exp 45, a power of the setting
        self.assertAlmostEqual(cli.curve_output(profile, "exp", 20), 0.0001 * 20 ** power, places=9)
        # Past the table nothing is measured: held at its last point, the least it can be.
        self.assertEqual([cli.curve_output(profile, "exp", n) for n in (55, 56, 100)], [0.2] * 3)
        self.assertEqual(number("exp", 21.9), 55)        # so the top of the band on exp is exp 55
        self.assertAlmostEqual(cli.lowest_output(profile), 0.01)

    def test_hysteresis(self):
        profile = cli.light_profile()
        pick = lambda output, now: cli.pick_curve(profile, output, now)
        self.assertEqual([pick(o, "linear") for o in (100, 20, 18, 17.9, 5)], ["linear"] * 3 + ["exp"] * 2)
        self.assertEqual([pick(o, "exp") for o in (1, 20, 21.9, 22, 50)], ["exp"] * 3 + ["linear"] * 2)
        self.assertEqual([pick(o, None) for o in (19.9, 20)], ["exp", "linear"])

    def test_a_raw_exp_100_is_never_left_brighter(self):
        # The light as calibration left it: exp 100, which is dimmer than linear 50.
        self.on("exp", 100)
        self.assertAlmostEqual(cli.output_of(cli.light_profile(), self.state()), 20)
        self.assertEqual(cli.describe(self.state(), cli.light_profile()), "on, white 5600K at 20% (exp 100)")
        self.run_command("white", "5000")                  # colour only: the same number on the same curve
        self.assertEqual(self.frames_of(FakeBridge.sent), [(100, "exp")])
        self.assertEqual((self.state()["kelvin"], self.state()["curve"], self.state()["brightness"]), (5000, "exp", 100))
        # The bedtime fade starts at exp 55 and stays on exp: no linear frame until the light is off.
        with open(cli.SUNDIAL_CONFIG_FILE, "w") as handle:
            handle.write("[sleep]\nwake = 07:00\nbedtime = 23:00\n")
        self.on("exp", 100)
        cli.write_json(cli.FOLLOW_MEMORY_FILE, {"stamp": cli.state_stamp(), "sent": cli.follow_look(self.state())})
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        finished, _ = cli.fade_out(light, cli.read_json(cli.FOLLOW_MEMORY_FILE, {}), 300.0, self.now)
        self.assertTrue(finished)
        off = FakeBridge.sent.index(cli.to_hex(cli.power_frame(cli.parse_mac(MAC), False)))
        frames = self.frames_of(FakeBridge.sent[:off])
        self.assertEqual({c for n, c in frames}, {"exp"})
        self.assertLessEqual(frames[0][0], 55)
        self.assertEqual([n for n, c in frames], sorted((n for n, c in frames), reverse=True))
        self.assertEqual(self.frames_of(FakeBridge.sent[off:]), [(100, "exp")])   # left for standby as it was
        self.assertEqual((self.state()["curve"], self.state()["brightness"]), ("exp", 100))
        # Switched on again (as Home does), it comes back to exp 100, from exp 1 since its curve is not known.
        FakeBridge.sent = []
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        light.show_white(5600, light.output())
        self.assertEqual(self.frames_of(FakeBridge.sent), [(1, "exp"), (100, "exp")])

    def test_exp_to_linear_is_one_frame(self):
        self.on("exp", 45)
        self.run_command("white", "5600", "50")
        self.assertEqual(self.frames_of(FakeBridge.sent), [(50, "linear")])
        self.assertEqual(self.waits, [])
        self.assertEqual((self.state()["curve"], self.state()["brightness"], self.state()["output"]), ("linear", 50, 50))

    def test_linear_to_exp_dips_first(self):
        self.on("linear", 50)
        self.run_command("white", "5600", "5")
        # An exp frame with the number the light shows now: the same light on
        # linear, then the curve changes, a dip. Then the target.
        self.assertEqual(self.frames_of(FakeBridge.sent), [(50, "exp"), (45, "exp")])
        self.assertEqual(self.waits, [1.5])
        self.assertEqual((self.state()["curve"], self.state()["brightness"]), ("exp", 45))

    def test_from_an_unknown_curve_to_exp_dips_too(self):
        cli.write_json(cli.STATE_FILE, {"on": True, "mode": "white", "kelvin": 5600, "brightness": 30})  # no curve
        self.run_command("white", "5600", "5")
        self.assertEqual(self.frames_of(FakeBridge.sent), [(30, "exp"), (45, "exp")])

    def test_within_one_curve_nothing_special(self):
        self.on("exp", 45)
        self.run_command("white", "5600", "0.5")
        self.assertEqual(self.frames_of(FakeBridge.sent), [(cli.curve_number(cli.light_profile(), "exp", 0.5), "exp")])
        self.on("linear", 50)
        self.run_command("brightness", "80")
        self.assertEqual(self.frames_of(FakeBridge.sent), [(80, "linear")])
        self.assertEqual(self.waits, [])

    def test_the_command_takes_output_and_a_raw_way(self):
        self.on("linear", 50)
        self.run_command("white", "5600", "0.01")
        self.assertEqual(self.frames_of(FakeBridge.sent)[-1], (1, "exp"))
        self.assertEqual(cli.describe(self.state()), "on, white 5600K at 0.01% (exp 1)")
        FakeBridge.sent = []
        self.run_command("white", "5600", "45", "--curve", "linear")   # raw: the number on that curve, no care taken
        self.assertEqual(self.frames_of(FakeBridge.sent), [(45, "linear")])
        self.assertEqual(self.state()["output"], 45)
        FakeBridge.sent = []
        self.run_command("white", "5600", "45", "--curve", "exp")
        self.assertEqual(self.frames_of(FakeBridge.sent), [(45, "exp")])
        self.assertEqual(cli.describe(self.state()), "on, white 5600K at 5% (exp 45)")
        FakeBridge.sent = []
        with self.assertRaises(cli.Failure):               # a raw setting is a whole number: 0.4 is not 0, dark
            self.run_command("white", "5600", "0.4", "--curve", "exp")
        cli.write_json(cli.LIGHT_FILE, {"mac": MAC})       # nor on a light with one curve, the CB200C
        with self.assertRaises(cli.Failure):
            self.run_command("brightness", "0.5")
        self.assertEqual(FakeBridge.sent, [])
        self.run_command("brightness", "3.0")
        self.assertEqual(self.frames_of(FakeBridge.sent)[-1][0], 3)

    def test_the_range_is_kept(self):
        with self.assertRaises(cli.Failure):
            self.run_command("white", "6600", "5")
        cli.write_json(cli.STATE_FILE, {"on": True, "mode": "white", "kelvin": 6600, "brightness": 5, "curve": "exp"})
        self.run_command("brightness", "4")
        self.assertEqual(self.state()["kelvin"], 6500)
        self.assertTrue(all(frame[22:24] == "41" for frame in FakeBridge.sent if frame[2:4] == "90"))

    def test_what_it_cannot_do_is_said_plainly(self):
        for words, said in ((["faint", "5"], "does not take the channels frame"),
                            (["channels", "1", "--cold", "255"], "does not take the channels frame"),
                            (["colour", "30", "60", "5"], "white LEDs only; the nearest white is"),
                            (["rgb", "ff8800", "5"], "white LEDs only")):
            with self.assertRaises(cli.Failure) as raised:
                self.run_command(*words)
            self.assertIn(said, str(raised.exception))
        self.assertEqual(FakeBridge.sent, [])

    def test_the_nearest_white(self):
        profile = cli.light_profile()
        for kelvin in (2700, 3500, 5000, 6500):
            self.assertEqual(cli.nearest_white(profile, *cli.white_hue_saturation(kelvin)), kelvin)
        self.assertEqual(cli.nearest_white(profile, 240, 100), 6500)

    def test_the_follower_works_in_output_on_both_curves(self):
        self.on("linear", 100, kelvin=6500)
        self.set_elsewhere(cli.read_json(cli.STATE_FILE, {}), minutes_ago=300)
        self.sun(NIGHT)
        self.assertIn("white 2700K at 5%", self.apply())
        self.assertEqual(self.frames_of(FakeBridge.sent), [(100, "exp"), (45, "exp")])  # night: 5% output on exp
        self.later(1)
        self.sun(NIGHT)
        self.assertIsNone(self.apply())                    # and not again every minute
        FakeBridge.sent = []
        self.later(1)
        self.sun(DAY)
        self.assertIn("white 6500K at 100%", self.apply())
        self.assertEqual(self.frames_of(FakeBridge.sent), [(100, "linear")])

    def test_a_ramp_through_the_threshold_changes_curve_once(self):
        self.on("exp", 45, kelvin=4000)
        self.set_elsewhere(cli.read_json(cli.STATE_FILE, {}), minutes_ago=300)
        curves = []
        for minute, daylight in enumerate([0.15, 0.16, 0.14, 0.17, 0.15, 0.13, 0.18, 0.2, 0.18, 0.17, 0.14, 0.1, 0.12]):
            self.later(1)
            self.sun({"sun_elevation": 5.0, "daylight": daylight, "kelvin": 4000})  # 19.25% ... 24% ... 14.5% output
            self.apply()
            curves.append(self.state()["curve"])
        changes = sum(1 for a, b in zip(curves, curves[1:]) if a != b)
        self.assertEqual(curves[0], "exp")
        self.assertIn("linear", curves)
        self.assertEqual(changes, 2)                       # up once at 22%, down once below 18%

    def test_the_bedtime_fade_crosses_down_with_a_dip(self):
        with open(cli.SUNDIAL_CONFIG_FILE, "w") as handle:
            handle.write("[sleep]\nwake = 07:00\nbedtime = 23:00\n")
        self.on("linear", 100, kelvin=2700)
        cli.write_json(cli.FOLLOW_MEMORY_FILE, {"stamp": cli.state_stamp(), "sent": cli.follow_look(self.state())})
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        finished, _ = cli.fade_out(light, cli.read_json(cli.FOLLOW_MEMORY_FILE, {}), 300.0, self.now)
        self.assertTrue(finished)
        off = FakeBridge.sent.index(cli.to_hex(cli.power_frame(cli.parse_mac(MAC), False)))
        frames = self.frames_of(FakeBridge.sent[:off])     # the fade itself, not the frame left for standby
        linear = [n for n, c in frames if c == "linear"]
        exp = [n for n, c in frames if c == "exp"]
        self.assertEqual(linear[:3], sorted(linear[:3], reverse=True))
        self.assertTrue(linear[-1] >= 18 and linear[-1] < 25)    # down on linear to the bottom of the band
        first_exp = frames.index((exp[0], "exp"))
        self.assertEqual(frames[first_exp - 1], (linear[-1], "linear"))
        self.assertEqual(exp[0], linear[-1])               # the dip: exp with the number shown on linear
        self.assertIn(1.5, self.waits)
        self.assertEqual(exp[1:], sorted(exp[1:], reverse=True))
        self.assertEqual(exp[-1], 1)                       # down to exp 1, about 0.01%
        self.assertIn(cli.to_hex(cli.power_frame(cli.parse_mac(MAC), False)), FakeBridge.sent)
        # The record keeps the setting from before the fade, and says the light's curve is not known.
        state = self.state()
        self.assertEqual((state["on"], state["curve"], state["brightness"]), (False, "linear", 100))
        self.assertIsNone(state["shown"]["curve"])
        # So the morning comes on with a dip, never a flash.
        FakeBridge.sent = []
        light = cli.Light(argparse.Namespace(bridge=None, mac=None, quiet=True))
        light.show_white(2800, 5)
        self.assertEqual(self.frames_of(FakeBridge.sent), [(1, "exp"), (45, "exp")])

    def test_the_sunrise_crosses_20_once_and_starts_below_1(self):
        profile = cli.light_profile()
        steps = [cli.sunrise_setting(t / 400.0, 100, 5600, "sky", profile) for t in range(401)]
        self.assertEqual({step[0] for step in steps}, {"white"})
        self.assertAlmostEqual(steps[0][2], 0.01)          # exp 1, far below 1%
        self.assertEqual(steps[-1], ("white", 5600, 100))
        state, curves, sent = {"on": True, "mode": "white", "curve": "exp", "brightness": 1, "kelvin": 5600}, [], []
        for step in steps:
            frames, look = cli.plan_white(profile, state, step[1], step[2])
            curves.append(look["curve"])
            sent += frames
            state.update(look)
        self.assertEqual(curves[0], "exp")
        self.assertEqual(curves[-1], "linear")
        self.assertEqual(sum(1 for a, b in zip(curves, curves[1:]) if a != b), 1)
        self.assertTrue(all(len(frames) == 1 for frames in [cli.plan_white(profile, dict(state, curve="exp"), 5600, 50)[0]]))
        glow = [cli.sunrise_setting(t / 100.0, 100, 5600, "glow", profile) for t in range(101)]
        self.assertAlmostEqual(glow[0][2], 0.01)
        self.assertEqual(glow[-1][2], 100)

    def test_the_sunrise_plan_shows_the_curves(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.run_command("sunrise", "--plan", "--minutes", "30")
        lines = out.getvalue().splitlines()
        self.assertIn("(exp 1)", lines[0])
        self.assertTrue(any("(linear 22)" in line for line in lines))
        self.assertIn("(linear 100)", lines[-1])

    def test_a_sunrise_sends_only_white_frames(self):
        sleeps = []
        original = cli.time.sleep
        cli.time.sleep = sleeps.append
        self.addCleanup(lambda: setattr(cli.time, "sleep", original))
        self.on("linear", 5)
        self.run_command("sunrise", "--minutes", "0.002", "--brightness", "5", "--kelvin", "5600")
        self.assertEqual({frame[2:4] for frame in FakeBridge.sent}, {"90", "8d"})
        self.assertEqual(self.frames_of(FakeBridge.sent)[-1], (45, "exp"))
        self.assertEqual(self.frames_of(FakeBridge.sent)[:2], [(5, "exp"), (1, "exp")])   # from linear 5: a dip

    def test_follow_on_at_midday_is_not_a_fresh_start(self):
        # The follower was off through the morning; the light is on, set by him.
        with open(cli.SUNDIAL_CONFIG_FILE, "w") as handle:
            handle.write("[sleep]\nwake = 07:00\nbedtime = 23:00\n")
        os.makedirs(cli.TRACKING_DIR, exist_ok=True)
        cli.write_json(os.path.join(cli.TRACKING_DIR, "settings.json"), {})
        cli.set_tracking(False, "home")
        cli.write_json(cli.FOLLOW_MEMORY_FILE, {"day": 0, "situation": "off"})
        cli.write_json(cli.STATE_FILE, {"on": True, "mode": "white", "kelvin": 5600, "brightness": 20, "curve": "exp"})
        self.run_command("follow", "off")
        noon = time.mktime(time.localtime()[:3] + (12, 0, 0, 0, 0, -1))
        original = cli.time.time
        cli.time.time = lambda: noon
        self.addCleanup(lambda: setattr(cli.time, "time", original))
        self.sun(DAY)
        os.utime(cli.SUNDIAL_STATE_FILE, (noon, noon))
        self.run_command("follow", "on")
        self.assertEqual(FakeBridge.sent, [])            # not 6500K at 100%
        self.assertEqual((self.state()["kelvin"], self.state()["brightness"]), (5600, 20))
        self.assertFalse(cli.tracking()["on"])

    def test_follow_on_leaves_sun_tracking_alone(self):
        os.makedirs(cli.TRACKING_DIR, exist_ok=True)
        cli.write_json(os.path.join(cli.TRACKING_DIR, "settings.json"), {})   # the switch in use
        cli.set_tracking(False, "home")
        self.run_command("follow", "off")
        self.run_command("follow", "on")
        self.assertTrue(cli.follow_settings()["enabled"])
        self.assertFalse(cli.tracking()["on"])
        self.assertEqual(FakeBridge.sent, [])            # tracking is off, so the light is left alone




class AS600BHomeKitTest(AS600BScaffold):
    """The Apple Home service's side, without HAP-python: the link that turns Home's values into frames."""

    def setUp(self):
        super().setUp()
        self.on("exp", 45)
        sys.modules["neewer_cli"] = cli
        loader = importlib.machinery.SourceFileLoader(
            "neewer_homekit", os.path.join(HERE, "..", "homekit", "neewer_homekit.py"))
        self.homekit = importlib.util.module_from_spec(importlib.util.spec_from_loader("neewer_homekit", loader))
        loader.exec_module(self.homekit)
        self.sleeps = []
        saved = self.homekit.time.sleep
        self.homekit.time.sleep = self.sleeps.append
        self.addCleanup(lambda: setattr(self.homekit.time, "sleep", saved))
        self.link = self.homekit.LightLink()

    def write(self, **values):
        self.link.apply_home(values)
        self.link.send(self.link.snapshot())

    def test_the_colour_temperature_range(self):
        self.assertEqual(self.homekit.mired_range(self.link.profile), (154, 370))
        self.assertEqual(self.homekit.mired_to_kelvin(154, self.link.profile), 6500)
        self.assertEqual(self.homekit.mired_to_kelvin(370, self.link.profile), 2700)

    def test_home_is_shown_light_output(self):
        self.assertEqual(self.link.home_brightness(self.link.snapshot()), 5)     # exp 45 is 5% output
        self.on("exp", 1)
        self.link = self.homekit.LightLink()
        self.assertEqual(self.link.home_brightness(self.link.snapshot()), 1)     # 0.01%, shown as 1 while lit

    def test_a_raw_exp_100_from_home(self):
        self.on("exp", 100)
        self.link = self.homekit.LightLink()
        self.assertEqual(self.link.home_brightness(self.link.snapshot()), 20)    # the least exp 100 can be
        self.write(ColorTemperature=250)                   # colour only: still exp 100
        self.assertEqual(self.frames_of(FakeBridge.sent), [(100, "exp")])
        self.assertEqual((self.state()["curve"], self.state()["brightness"]), ("exp", 100))

    def test_the_slider_sets_output_across_the_whole_range(self):
        self.write(Brightness=80)
        self.assertEqual(self.frames_of(FakeBridge.sent), [(80, "linear")])     # exp -> linear: one frame
        FakeBridge.sent = []
        self.write(Brightness=10)
        self.assertEqual(self.frames_of(FakeBridge.sent), [(80, "exp"), (50, "exp")])  # linear -> exp: a dip first
        self.assertEqual(self.sleeps[-1], 1.5)
        self.assertEqual(self.state()["output"], 10)
        self.assertEqual(self.state()["curve"], "exp")

    def test_home_writing_back_what_it_was_shown_changes_nothing(self):
        self.on("exp", 1)
        self.link = self.homekit.LightLink()
        self.link.apply_home({"On": True, "Brightness": 1})
        self.assertAlmostEqual(self.link.snapshot()["output"], 0.01)

    def test_a_colour_from_home_becomes_the_nearest_white(self):
        self.write(Hue=240, Saturation=100)
        self.assertEqual(self.link.snapshot()["kelvin"], 6500)
        self.assertEqual(self.frames_of(FakeBridge.sent)[-1], (45, "exp"))
        self.assertTrue(all(frame[2:4] in ("90", "8d") for frame in FakeBridge.sent))

    def test_a_fresh_start_sends_output_with_the_hybrid(self):
        cli.write_json(cli.STATE_FILE, {"on": False, "mode": "white", "kelvin": 4000, "brightness": 50, "curve": "linear"})
        self.sun(NIGHT)
        self.link = self.homekit.LightLink()
        FakeBridge.sent = []
        self.write(On=True)
        self.assertEqual(self.frames_of(FakeBridge.sent), [(50, "exp"), (45, "exp")])   # 5% at night, with a dip
        self.assertEqual((self.state()["kelvin"], self.state()["output"]), (2700, 5))


if __name__ == "__main__":
    unittest.main()
