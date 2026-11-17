#!/usr/bin/env python3
"""Checks `neewer follow` without a light or a bridge.

Run: python3 tests/test_follow.py

Each test gets an empty settings folder and a made-up sundial state file. The
bridge is replaced by one that only records the frames it is given.
"""

import argparse
import importlib.machinery
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
            "SUNDIAL_CONFIG_FILE", "Bridge", "pause")}
        self.addCleanup(lambda: [setattr(cli, name, value) for name, value in self.saved.items()])
        for name, file in (("LIGHT_FILE", "light.json"), ("STATE_FILE", "state.json"),
                           ("FOLLOW_FILE", "follow.json"), ("FOLLOW_MEMORY_FILE", "follow-state.json"),
                           ("SUNDIAL_STATE_FILE", "sundial.json"),
                           # No sundial settings: no bedtime, so these tests pass at any hour.
                           ("SUNDIAL_CONFIG_FILE", "sundial.ini")):
            setattr(cli, name, os.path.join(self.folder.name, file))
        cli.Bridge = FakeBridge
        FakeBridge.sent, FakeBridge.broken = [], False
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


class BedtimeTest(Scaffold):
    """The bedtime rule, with sundial saying bedtime 23:00 and wake 07:00."""

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


if __name__ == "__main__":
    unittest.main()
