#!/usr/bin/env python3
"""Checks the desk's Apple Home service, and sundial's manual mode, without displays or a keyboard.

Run: python3 tests/test_desk.py

sundial here is monitors/sundial, the copy kept for reference of the one in
~/bin. It runs for real, against a state file in a temporary folder, with the
two functions that touch the displays replaced by ones that only take notes.
"""

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(HERE, "..", file))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


sundial = load("sundial_for_tests", "monitors/sundial")
desk = load("desk_homekit", "desk/desk_homekit.py")


class FakeSundial:
    """sundial itself, with pretend displays."""

    def __init__(self):
        self.count = 3
        self.slept = []

    def state(self):
        return sundial.load_state()

    def manual(self, arguments):
        sundial.main(["manual"] + arguments)
        return True

    def resume(self):
        sundial.main(["resume"])

    def displays(self):
        return self.count

    def sleep_displays(self, asleep):
        self.slept.append(asleep)


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        saved = [(module, name, getattr(module, name)) for module, name in (
            (sundial, "STATE"), (sundial, "CONFIG"), (sundial, "set_brightness"), (sundial, "set_kelvin"),
            (sundial, "print"), (desk, "APP_DIR"), (desk, "SUNDIAL_STATE_FILE"), (desk, "CONTROL_FILE"), (desk, "TRACKING_DIR"))
            if hasattr(module, name)]
        self.addCleanup(lambda: [setattr(module, name, value) for module, name, value in saved])
        sundial.STATE = os.path.join(self.folder.name, "sundial", "state.json")
        sundial.CONFIG = os.path.join(self.folder.name, "sundial.ini")
        sundial.print = lambda *args, **kwargs: None
        self.sent = []  # ("brightness", 40) and ("kelvin", 3000), in order
        sundial.set_brightness = lambda value: self.sent.append(("brightness", value)) or 3
        sundial.set_kelvin = lambda value: self.sent.append(("kelvin", value)) or True
        desk.APP_DIR = os.path.join(self.folder.name, "desk")
        desk.SUNDIAL_STATE_FILE = sundial.STATE
        desk.CONTROL_FILE = os.path.join(self.folder.name, "wooting", "control")
        # These tests are of the older way, where a change from Home is held
        # for a time. TrackingTest is of the switch.
        desk.TRACKING_DIR = os.path.join(self.folder.name, "tracking")
        desk.write_json(os.path.join(desk.TRACKING_DIR, "settings.json"), {"switch": False})
        self.sun = FakeSundial()
        sundial.main([])  # the minute timer has run once
        self.sent = []

    def state(self):
        return sundial.load_state()

    def expire(self):
        """As if the hold had run out: the override's end is moved into the past."""
        state = self.state()
        state["override"]["until"] = (datetime.now().astimezone() - timedelta(minutes=1)).isoformat(timespec="seconds")
        sundial.save_state(state)

    def target(self):
        return sundial.target(datetime.now().astimezone(), sundial.load_config())


class SundialManualTest(Case):
    def test_a_manual_value_is_applied_and_held(self):
        sundial.main(["manual", "60", "brightness=40", "kelvin=3000"])
        self.assertEqual(self.sent, [("brightness", 40), ("kelvin", 3000)])
        self.assertEqual((self.state()["applied"]["brightness"], self.state()["applied"]["kelvin"]), (40, 3000))
        self.sent = []
        sundial.main([])  # the next minute: nothing changes, nothing is sent
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state()["applied"]["brightness"], 40)

    def test_the_target_stays_the_suns(self):
        want = self.target()
        sundial.main(["manual", "brightness=77", "kelvin=5000"])
        target = self.state()["target"]
        self.assertEqual((target["brightness"], target["kelvin"]), (want["brightness"], want["kelvin"]))
        self.assertEqual(set(target), {"sun_elevation", "daylight", "bedtime", "brightness", "kelvin"})

    def test_a_value_not_given_follows_the_sun(self):
        want = self.target()
        sundial.main(["manual", "brightness=40"])
        self.assertEqual(self.state()["applied"]["kelvin"], want["kelvin"])
        self.assertNotIn("kelvin", self.state()["override"])

    def test_later_values_join_earlier_ones(self):
        sundial.main(["manual", "brightness=40"])
        sundial.main(["manual", "kelvin=3000"])
        override = self.state()["override"]
        self.assertEqual((override["mode"], override["brightness"], override["kelvin"]), ("manual", 40, 3000))
        sundial.main(["manual", "brightness=sun"])
        self.assertNotIn("brightness", self.state()["override"])
        self.assertEqual(self.state()["applied"]["brightness"], self.target()["brightness"])

    def test_it_runs_out(self):
        want = self.target()
        sundial.main(["manual", "brightness=77", "kelvin=5000"])
        self.expire()
        sundial.main([])
        self.assertNotIn("override", self.state())
        self.assertEqual(self.state()["applied"]["brightness"], want["brightness"])
        self.assertEqual(self.state()["applied"]["kelvin"], want["kelvin"])

    def test_resume_ends_it(self):
        sundial.main(["manual", "brightness=77"])
        sundial.main(["resume"])
        self.assertNotIn("override", self.state())
        self.assertEqual(self.state()["applied"]["brightness"], self.target()["brightness"])

    def test_the_default_hold_and_limits(self):
        sundial.main(["manual", "brightness=140", "kelvin=9000"])
        override = self.state()["override"]
        self.assertEqual((override["brightness"], override["kelvin"]), (100, 6500))
        left = datetime.fromisoformat(override["until"]) - datetime.now().astimezone()
        self.assertAlmostEqual(left.total_seconds(), 120 * 60, delta=5)
        with self.assertRaises(SystemExit):
            sundial.main(["manual", "volume=3"])

    def test_pause_and_movie_still_work_and_replace_it(self):
        sundial.main(["manual", "brightness=40"])
        sundial.main(["movie"])
        self.assertEqual(self.state()["override"]["mode"], "movie")
        self.assertNotIn("brightness", self.state()["override"])
        sundial.main(["pause", "30"])
        self.assertEqual(self.state()["override"]["mode"], "pause")
        self.assertEqual(self.state()["applied"]["kelvin"], 6500)
        sundial.main(["status"])


class MonitorsTest(Case):
    def setUp(self):
        super().setUp()
        self.monitors = desk.Monitors(self.sun)

    def test_home_is_shown_what_sundial_applied(self):
        want = self.target()
        view = self.monitors.view()
        self.assertEqual((view["on"], view["brightness"]), (True, want["brightness"]))
        self.assertEqual(view["kelvin"], max(2700, want["kelvin"]))
        self.assertEqual(desk.home_values({"on": True, "brightness": 40, "kelvin": 2700}),
                         {"On": True, "Brightness": 40, "ColorTemperature": 370})

    def test_brightness_and_colour_from_home(self):
        self.monitors.set({"Brightness": 40})
        self.assertEqual(self.sent, [("brightness", 40), ("kelvin", self.target()["kelvin"])])
        self.monitors.set({"ColorTemperature": 250})
        self.assertEqual(self.sent[-2:], [("brightness", 40), ("kelvin", 4000)])
        view = self.monitors.view()
        self.assertEqual((view["brightness"], view["kelvin"]), (40, 4000))
        left = datetime.fromisoformat(self.state()["override"]["until"]) - datetime.now().astimezone()
        self.assertAlmostEqual(left.total_seconds(), 120 * 60, delta=5)

    def test_the_colour_range_is_night_shifts(self):
        self.assertEqual((desk.MIRED_MIN, desk.MIRED_MAX), (154, 370))
        self.assertEqual(desk.mired_to_kelvin(370), 2700)
        self.assertEqual(desk.mired_to_kelvin(167), 6000)
        self.assertEqual(desk.mired_to_kelvin(160), 6500)  # cooler than Night Shift goes: no tint
        self.assertEqual(desk.mired_to_kelvin(500), 2700)

    def test_the_hold_runs_out_and_home_shows_the_sun_again(self):
        self.monitors.set({"Brightness": 40, "ColorTemperature": 250})
        self.expire()
        sundial.main([])
        want = self.target()
        view = self.monitors.view()
        self.assertEqual((view["brightness"], view["kelvin"]), (want["brightness"], max(2700, want["kelvin"])))

    def test_off_is_brightness_nought_and_on_brings_the_brightness_back(self):
        self.monitors.set({"Brightness": 40})
        self.monitors.set({"On": False})
        self.assertEqual(self.state()["applied"]["brightness"], 0)
        self.assertEqual(self.monitors.view(), {"on": False, "brightness": 0, "kelvin": self.monitors.view()["kelvin"]})
        self.assertEqual(self.sun.slept, [])  # the displays are not put to sleep
        self.monitors.set({"On": True})
        self.assertEqual(self.state()["applied"]["brightness"], 40)
        self.assertTrue(self.monitors.view()["on"])

    def test_on_after_off_goes_back_to_the_sun_if_nothing_was_held(self):
        self.monitors.set({"On": False})
        self.monitors.set({"On": True})
        self.assertNotIn("brightness", self.state()["override"])
        self.assertEqual(self.state()["applied"]["brightness"], self.target()["brightness"])

    def test_off_ends_when_sundial_lets_go(self):
        self.monitors.set({"On": False})
        sundial.main(["resume"])
        self.assertTrue(self.monitors.view()["on"])
        self.monitors.set({"On": False})
        self.expire()
        sundial.main([])
        self.assertTrue(self.monitors.view()["on"])

    def test_a_second_on_does_not_disturb_a_pause(self):
        sundial.main(["pause", "30"])
        self.monitors.set({"On": True})
        self.assertEqual(self.state()["override"]["mode"], "pause")
        self.monitors.set({"Brightness": 30})  # a real change does replace it
        self.assertEqual(self.state()["override"]["mode"], "manual")

    def test_off_can_mean_sleep(self):
        desk.write_json(desk.path("desk.json"), {"monitors": {"off_means": "sleep", "hold_minutes": 30}})
        self.monitors.set({"On": False})
        self.assertEqual((self.sun.slept, self.sent), ([True], []))
        self.assertFalse(self.monitors.view()["on"])
        self.monitors.set({"On": True})
        self.assertEqual(self.sun.slept, [True, False])
        self.assertTrue(self.monitors.view()["on"])

    def test_no_displays_is_no_response(self):
        self.assertTrue(self.monitors.reachable())
        self.sun.count = 0
        self.monitors.counted = None
        self.assertFalse(self.monitors.reachable())

    def test_settings_are_read(self):
        desk.write_json(desk.path("desk.json"), {"monitors": {"hold_minutes": 30, "name": 7}, "port": "x"})
        chosen = desk.settings()
        self.assertEqual((chosen["monitors"]["hold_minutes"], chosen["monitors"]["name"], chosen["port"]),
                         (30, "Monitors", 51828))
        self.monitors.set({"Brightness": 40})
        left = datetime.fromisoformat(self.state()["override"]["until"]) - datetime.now().astimezone()
        self.assertAlmostEqual(left.total_seconds(), 30 * 60, delta=5)


class KeyboardTest(Case):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.keyboard = desk.Keyboard(self.sun, clock=lambda: self.now)

    def sky(self, daylight, kelvin):
        state = self.state()
        state["target"].update(daylight=daylight, kelvin=kelvin)
        sundial.save_state(state)

    def control(self):
        with open(desk.CONTROL_FILE) as handle:
            return handle.read()

    def test_night_is_dim_and_warm(self):
        self.sky(0.0, 2700)
        self.assertEqual(self.keyboard.sync(), {"on": True, "brightness": 25, "kelvin": 2700})
        lines = self.control().splitlines()
        self.assertEqual(lines[:2], ["on 1", "brightness 0.250"])
        red, green, blue = (float(part) for part in lines[2].split()[1:])
        self.assertEqual(red, 1.0)
        self.assertTrue(blue < green < red and blue < 0.5)
        self.assertEqual(lines[3], "base ffffff")

    def test_full_day_hands_the_keys_back_to_the_profile(self):
        self.sky(1.0, 6500)
        self.assertEqual(self.keyboard.sync(), {"on": True, "brightness": 100, "kelvin": 6500})
        self.assertEqual(self.control(), "on 1\nbrightness 1.000\ntint 1.000 1.000 1.000\nbase profile\n")
        desk.write_json(desk.path("desk.json"), {"keyboard": {"keep_profile_at_full": False, "base_colour": "ff8800"}})
        self.keyboard.sync()
        self.assertIn("base ff8800", self.control())

    def test_between_night_and_day(self):
        self.sky(0.4, 4130)
        view = self.keyboard.sync()
        self.assertEqual((view["brightness"], view["kelvin"]), (55, 4100))

    def test_it_follows_the_sun_not_a_monitor_override(self):
        self.sky(0.0, 2700)
        sundial.main(["manual", "brightness=90", "kelvin=6000"])
        self.sky(0.0, 2700)  # the test's sky, as sundial's own target would be the real one
        self.assertEqual(self.keyboard.sync(), {"on": True, "brightness": 25, "kelvin": 2700})

    def test_a_change_from_home_is_held_then_let_go(self):
        self.sky(0.0, 2700)
        self.keyboard.set({"Brightness": 60})
        self.assertEqual(self.keyboard.view(), {"on": True, "brightness": 60, "kelvin": 2700})
        self.assertIn("brightness 0.600", self.control())
        self.keyboard.set({"ColorTemperature": 200})
        self.assertEqual(self.keyboard.view(), {"on": True, "brightness": 60, "kelvin": 5000})
        self.now += 119 * 60
        self.assertEqual(self.keyboard.sync()["brightness"], 60)
        self.now += 2 * 60
        self.assertEqual(self.keyboard.sync(), {"on": True, "brightness": 25, "kelvin": 2700})
        self.assertIn("brightness 0.250", self.control())

    def test_off_is_every_key_dark(self):
        self.sky(0.0, 2700)
        self.keyboard.set({"On": False})
        self.assertTrue(self.control().startswith("on 0\n"))
        self.assertFalse(self.keyboard.view()["on"])
        self.keyboard.set({"On": True})
        self.assertTrue(self.control().startswith("on 1\n"))

    def test_the_file_is_only_written_when_it_changes(self):
        self.sky(0.0, 2700)
        self.keyboard.sync()
        stamp = os.stat(desk.CONTROL_FILE).st_mtime_ns
        os.utime(desk.CONTROL_FILE, ns=(stamp - 10**9, stamp - 10**9))
        self.keyboard.sync()
        self.assertEqual(os.stat(desk.CONTROL_FILE).st_mtime_ns, stamp - 10**9)

    def test_without_sundial_it_keeps_the_last_it_knew(self):
        self.sky(0.0, 2700)
        self.keyboard.sync()
        os.utime(sundial.STATE, (self.now - 3600, self.now - 3600))  # sundial has stopped
        self.sky_was = self.keyboard.sync()
        self.assertEqual(self.sky_was, {"on": True, "brightness": 25, "kelvin": 2700})

    def test_tint(self):
        self.assertEqual(desk.tint(6500), (1.0, 1.0, 1.0))
        warm = desk.tint(2700)
        self.assertEqual(warm[0], 1.0)
        self.assertTrue(0.55 < warm[1] < 0.75 and 0.2 < warm[2] < 0.45)


class TrackingTest(Case):
    """The sun tracking switch, as the monitors and the keyboard keep to it."""

    def setUp(self):
        super().setUp()
        os.remove(os.path.join(desk.TRACKING_DIR, "settings.json"))
        self.now = time.time()
        self.monitors = desk.Monitors(self.sun)
        self.keyboard = desk.Keyboard(self.sun, clock=lambda: self.now)

    def track(self, on, by="home"):
        desk.write_json(os.path.join(desk.TRACKING_DIR, "tracking.json"),
                        {"on": on, "since": time.time(), "by": by, "why": ""})

    def tracking(self):
        return desk.read_json(os.path.join(desk.TRACKING_DIR, "tracking.json"), {})

    def sky(self, daylight, kelvin):
        state = self.state()
        state["target"].update(daylight=daylight, kelvin=kelvin)
        sundial.save_state(state)

    def test_a_brightness_from_home_turns_tracking_off_and_is_held_with_no_end(self):
        self.monitors.set({"Brightness": 40})
        self.assertEqual((self.tracking()["on"], self.tracking()["by"]), (False, "monitors"))
        left = datetime.fromisoformat(self.state()["override"]["until"]) - datetime.now().astimezone()
        self.assertGreater(left.days, 3000)
        self.monitors.react()
        self.assertEqual(self.state()["applied"]["brightness"], 40)

    def test_off_and_on_from_home_do_not(self):
        self.monitors.set({"On": False})
        self.monitors.react()
        self.monitors.set({"On": True})
        self.monitors.react()
        self.assertTrue(desk.tracking_on())
        self.keyboard.set({"On": False})
        self.keyboard.react()
        self.assertTrue(desk.tracking_on())
        self.assertFalse(self.keyboard.view()["on"])

    def test_the_monitors_are_held_as_they_are_while_tracking_is_off(self):
        before = dict(self.state()["applied"])
        self.track(False, "light")                       # a light was set by hand
        self.monitors.react()
        override = self.state()["override"]
        self.assertEqual((override["mode"], override["brightness"], override["kelvin"]),
                         ("manual", before["brightness"], before["kelvin"]))
        self.sent = []
        self.monitors.react()                            # once is enough
        sundial.main([])
        self.assertEqual(self.sent, [])
        self.track(True)
        self.monitors.react()
        self.assertNotIn("override", self.state())       # sundial follows the sun again

    def test_sundial_manual_by_hand_turns_tracking_off(self):
        sundial.main(["manual", "60", "brightness=55"])
        self.monitors.react()
        self.assertEqual((self.tracking()["on"], self.tracking()["by"]), (False, "monitors"))
        self.monitors.react()                            # and the 60 minutes become no end
        left = datetime.fromisoformat(self.state()["override"]["until"]) - datetime.now().astimezone()
        self.assertGreater(left.days, 3000)
        self.assertEqual(self.state()["override"]["brightness"], 55)

    def test_monitors_switched_off_stay_off_when_tracking_comes_on(self):
        self.monitors.set({"On": False})
        self.track(False, "light")
        self.monitors.react()
        self.track(True)
        self.monitors.react()
        self.assertFalse(self.monitors.view()["on"])
        self.assertEqual(self.state()["applied"]["brightness"], 0)
        self.assertNotIn("kelvin", self.state()["override"])   # the colour follows the sun
        self.monitors.react()
        self.assertTrue(desk.tracking_on())              # and that hold is not taken for a setting

    def test_a_pause_is_left_alone(self):
        sundial.main(["pause", "30"])
        self.monitors.react()
        self.assertTrue(desk.tracking_on())
        self.assertEqual(self.state()["override"]["mode"], "pause")

    def test_the_keyboard_is_held_as_it_is_and_let_go(self):
        self.sky(0.0, 2700)
        self.assertEqual(self.keyboard.sync()["brightness"], 25)
        self.track(False, "light")
        self.keyboard.react()
        self.sky(1.0, 6500)                              # the sun moves on; the keyboard does not
        self.now += 6 * 3600
        self.assertEqual(self.keyboard.sync(), {"on": True, "brightness": 25, "kelvin": 2700})
        self.track(True)
        self.keyboard.react()
        os.utime(sundial.STATE, (self.now, self.now))    # sundial has kept running
        self.assertEqual(self.keyboard.sync(), {"on": True, "brightness": 100, "kelvin": 6500})

    def test_a_keyboard_brightness_from_home_turns_tracking_off(self):
        self.sky(0.0, 2700)
        self.keyboard.set({"Brightness": 60})
        self.assertEqual((self.tracking()["on"], self.tracking()["by"]), (False, "keyboard"))
        self.keyboard.react()
        self.now += 10 * 3600
        self.assertEqual(self.keyboard.sync()["brightness"], 60)   # no timer

    def test_the_old_way_is_still_there(self):
        desk.write_json(os.path.join(desk.TRACKING_DIR, "settings.json"), {"switch": False})
        self.monitors.set({"Brightness": 40})
        self.monitors.react()
        self.assertTrue(desk.tracking_on())
        left = datetime.fromisoformat(self.state()["override"]["until"]) - datetime.now().astimezone()
        self.assertAlmostEqual(left.total_seconds(), 120 * 60, delta=5)


class LinkTest(Case):
    def test_a_burst_collapses(self):
        calls = []

        class Device:
            def set(self, values):
                calls.append(dict(values))
        link = desk.Link(Device())
        for level in (10, 20, 30):
            link.write({"Brightness": level})
        link.write({"ColorTemperature": 250})
        self.assertTrue(link.busy())
        link.flush()
        self.assertEqual(calls, [{"Brightness": 30, "ColorTemperature": 250}])
        self.assertFalse(link.busy())

    def test_pairing_files_are_private(self):
        pairing = desk.load_or_create_pairing()
        self.assertRegex(pairing["setup_code"], r"^\d{3}-\d{2}-\d{3}$")
        self.assertEqual(os.stat(desk.pairing_file()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(desk.data_dir()).st_mode & 0o777, 0o700)
        self.assertEqual(desk.load_or_create_pairing(), pairing)


if __name__ == "__main__":
    unittest.main()
