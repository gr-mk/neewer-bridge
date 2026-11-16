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


class FollowTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.saved = {name: getattr(cli, name) for name in (
            "LIGHT_FILE", "STATE_FILE", "FOLLOW_FILE", "FOLLOW_MEMORY_FILE", "SUNDIAL_STATE_FILE", "Bridge")}
        self.addCleanup(lambda: [setattr(cli, name, value) for name, value in self.saved.items()])
        for name, file in (("LIGHT_FILE", "light.json"), ("STATE_FILE", "state.json"),
                           ("FOLLOW_FILE", "follow.json"), ("FOLLOW_MEMORY_FILE", "follow-state.json"),
                           ("SUNDIAL_STATE_FILE", "sundial.json")):
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


if __name__ == "__main__":
    unittest.main()
