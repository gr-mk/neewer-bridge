#!/usr/bin/env python3
"""Checks the `fixture` command and its Apple Home mapping without real bulbs.

Run: python3 tests/test_fixture.py

Each test gets an empty settings folder, a made-up sundial state file, and a
made-up network of seven bulbs that answer the way the real ones do.
"""

import importlib.machinery
import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True


def load(name, file):
    loader = importlib.machinery.SourceFileLoader(name, os.path.join(HERE, "..", "fixture", file))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    sys.modules[name] = module
    loader.exec_module(module)
    return module


cli = load("fixture_cli", "fixture")
homekit = load("fixture_homekit", "fixture_homekit.py")

MACS = ["aabbcc00000%d" % n for n in range(1, 8)]
DAY = {"sun_elevation": 30.0, "daylight": 1.0, "kelvin": 6500}
DUSK = {"sun_elevation": 0.4, "daylight": 0.25, "kelvin": 4130}
NIGHT = {"sun_elevation": -30.0, "daylight": 0.0, "kelvin": 3400}


class Network:
    """Seven pretend bulbs. Replaces the UDP socket."""

    def __init__(self):
        self.bulbs = {"10.0.0.%d" % (n + 11): {"mac": mac, "state": True, "sceneId": 0, "temp": 4000, "dimming": 80}
                      for n, mac in enumerate(MACS)}
        self.down = set()   # addresses that do not answer
        self.drop = {}      # address: how many packets to lose first
        self.sent = []      # (address, method, values), in order
        self.searches = 0
        self.deaf = False   # True: no bulb hears a broadcast
        self.queue = []

    def bulb(self, mac):
        return next(bulb for bulb in self.bulbs.values() if bulb["mac"] == mac)

    def address(self, mac):
        return next(address for address, bulb in self.bulbs.items() if bulb["mac"] == mac)

    def lit(self):
        return [bulb["mac"] for bulb in self.bulbs.values() if bulb["state"]]

    def packets(self, method, mac=None):
        return [values for address, name, values in self.sent
                if name == method and (mac is None or address == self.address(mac))]

    # -- the socket

    def setsockopt(self, *args):
        pass

    def settimeout(self, seconds):
        pass

    def close(self):
        self.queue = []

    def sendto(self, data, where):
        address = where[0]
        message = json.loads(data)
        method, values = message["method"], message["params"]
        if address.endswith(".255"):
            self.searches += address == "255.255.255.255"
            targets = [] if self.deaf else list(self.bulbs)
        else:
            if method != "getPilot" or address in self.bulbs:
                self.sent.append((address, method, values))
            targets = [address] if address in self.bulbs else []
        for target in targets:
            if target in self.down:
                continue
            if self.drop.get(target, 0) > 0:
                self.drop[target] -= 1
                continue
            self.queue.append((json.dumps(self.answer(self.bulbs[target], method, values)).encode(), (target, 38899)))

    def recvfrom(self, size):
        if not self.queue:
            raise socket.timeout()
        return self.queue.pop(0)

    @staticmethod
    def answer(bulb, method, values):
        if method == "getPilot":
            return {"method": method, "env": "", "result": dict(bulb, rssi=-40)}
        if method != "setPilot":
            return {"method": method, "env": "", "error": {"code": -32601}}
        colour = any(key in values for key in "rgb")
        if "temp" in values:
            for key in "rgbcw":
                bulb.pop(key, None)
            bulb["temp"] = values["temp"]
        elif colour:
            bulb.pop("temp", None)
            bulb.update({key: values.get(key, 0) for key in "rgb"}, c=0, w=0)
        if "dimming" in values:
            bulb["dimming"] = values["dimming"]
        if "r" in bulb:
            bulb["dimming"] = max(10, bulb["dimming"])  # the real bulbs do this
        # A real bulb that is given a look without "state" switches on.
        bulb["state"] = bool(values.get("state", True))
        return {"method": method, "env": "", "result": {"success": True}}


class Case(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        saved = {name: getattr(cli, name) for name in (
            "APP_DIR", "SUNDIAL_STATE_FILE", "open_socket", "own_address", "WAITS", "SEARCH_WAIT")}
        self.addCleanup(lambda: [setattr(cli, name, value) for name, value in saved.items()])
        cli.APP_DIR = self.folder.name
        cli.SUNDIAL_STATE_FILE = os.path.join(self.folder.name, "sundial.json")
        self.net = Network()
        cli.open_socket = lambda: self.net
        cli.own_address = lambda: "10.0.0.2"
        cli.WAITS, cli.SEARCH_WAIT = (0.01,) * 4, 0.01
        cli.write_json(cli.path("fixture.json"), {"bulbs": MACS, "min_dimming": 1, "min_colour_dimming": 10,
                                                  "kelvin_min": 2200, "kelvin_max": 6500})
        self.fixture = cli.Fixture()
        self.now = time.time()
        self.sun(DAY)

    def sun(self, target, minutes=0):
        """Sets the sun, `minutes` from now. Returns that moment."""
        now = self.now + minutes * 60
        cli.write_json(cli.SUNDIAL_STATE_FILE, {"target": target})
        os.utime(cli.SUNDIAL_STATE_FILE, (now, now))
        return now

    def set(self, **changes):
        setting = dict(cli.DEFAULT_SETTING, on=True)
        setting.update(changes)
        return self.fixture.apply(setting, "cli")

    def setting(self):
        return self.fixture.reconcile()[0]["setting"]

    def shown(self):
        """(bulbs lit, their dimming values) as the bulbs themselves have it."""
        lit = [bulb for bulb in self.net.bulbs.values() if bulb["state"]]
        return len(lit), sorted({bulb["dimming"] for bulb in lit})


RING = MACS[1:]  # the stand-in layout: the first bulb is the centre


class SplitTest(unittest.TestCase):
    def test_a_whole_number_is_every_bulb_at_that_dimming(self):
        for brightness in range(1, 101):
            singled, _, others = cli.split(brightness, 7, 1)
            self.assertEqual((singled, others), (0, brightness))

    def test_below_one_percent_lights_fewer_bulbs(self):
        for bulbs in range(1, 7):
            self.assertEqual(cli.split(bulbs / 7.0, 7, 1), (bulbs, 1, 0))
        self.assertEqual(cli.split(0.4, 7, 1), (3, 1, 0))
        self.assertEqual(cli.split(0.001, 7, 1), (1, 1, 0))  # never none

    def test_a_fraction_puts_some_bulbs_one_step_higher(self):
        self.assertEqual(cli.split(12.5, 7, 1), (4, 13, 12))
        self.assertEqual(cli.split(1.5, 7, 1), (3, 2, 1))
        self.assertEqual(cli.split(99.9, 7, 1), (6, 100, 99))

    def test_the_light_rises_one_step_at_a_time(self):
        light = []
        for units in range(1, 701):
            singled, dimming, others = cli.split(units / 7.0, 7, 1)
            light.append(singled * dimming + (7 - singled) * others)
            self.assertTrue(dimming <= 100 or singled == 0)
        self.assertEqual(light, list(range(1, 701)))

    def test_a_colour_cannot_go_below_ten(self):
        self.assertEqual(cli.split(10, 7, 10), (0, 11, 10))
        self.assertEqual(cli.split(10.5, 7, 10), (4, 11, 10))
        self.assertEqual(cli.split(5, 7, 10), (4, 10, 0))   # 35 units: nearest to four bulbs at 10
        self.assertEqual(cli.split(1, 7, 10), (1, 10, 0))
        self.assertEqual(cli.split(9.9, 7, 10), (7, 10, 0))

    def test_percent(self):
        self.assertEqual([cli.percent(b) for b in (0.43, 1.0, 12.5, 37.57, 100)],
                         ["0.4%", "1%", "12.5%", "37.6%", "100%"])


class PatternTest(Case):
    def layout(self, centre, ring, **more):
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved.update(centre=centre, ring=ring, **more)
        cli.write_json(cli.path("fixture.json"), saved)

    def test_the_patterns_are_symmetric(self):
        centre, ring = MACS[3], [MACS[6], MACS[0], MACS[5], MACS[1], MACS[4], MACS[2]]
        self.layout(centre, ring, layout_mapped=True)
        expected = {
            0: [],
            1: [centre],
            2: [ring[0], ring[3]],
            3: [ring[0], ring[2], ring[4]],
            4: [centre, ring[0], ring[2], ring[4]],
            5: [centre, ring[0], ring[1], ring[3], ring[4]],
            6: ring,
            7: [centre] + ring,
        }
        for number, bulbs in expected.items():
            self.assertEqual(sorted(self.fixture.singled(number)), sorted(bulbs), number)

    def test_the_list_order_stands_in_until_the_layout_is_mapped(self):
        self.assertEqual(self.fixture.layout(), (MACS[0], RING))
        self.assertEqual(self.fixture.singled(2), [RING[0], RING[3]])
        self.layout(MACS[0], RING[:4])  # not a whole layout
        self.assertEqual(self.fixture.layout(), (MACS[0], RING))
        self.layout("zz", RING)
        self.assertEqual(self.fixture.layout(), (MACS[0], RING))

    def test_a_new_layout_is_used_without_starting_again(self):
        self.set(brightness=1 / 7.0)
        self.assertEqual(self.net.lit(), [MACS[0]])
        self.layout(MACS[2], [MACS[0], MACS[1], MACS[3], MACS[4], MACS[5], MACS[6]], layout_mapped=True)
        self.set(brightness=1 / 7.0)
        self.assertEqual(self.net.lit(), [MACS[2]])

    def test_below_one_percent_on_the_bulbs(self):
        for number, bulbs in ((1, [MACS[0]]), (2, [RING[0], RING[3]]), (3, [RING[0], RING[2], RING[4]]),
                              (4, [MACS[0], RING[0], RING[2], RING[4]]), (6, RING)):
            self.set(brightness=number / 7.0)
            self.assertEqual(sorted(self.net.lit()), sorted(bulbs))
            self.assertEqual(self.shown(), (number, [1]))

    def test_the_extra_step_goes_to_the_pattern_too(self):
        self.set(brightness=12 + 5 / 7.0)
        higher = sorted(bulb["mac"] for bulb in self.net.bulbs.values() if bulb["dimming"] == 13)
        self.assertEqual(higher, sorted([MACS[0], RING[0], RING[1], RING[3], RING[4]]))
        self.assertEqual(self.shown(), (7, [12, 13]))

    def test_other_numbers_of_bulbs_use_the_list_order(self):
        cli.write_json(cli.path("fixture.json"), {"bulbs": MACS[:4], "min_dimming": 1})
        self.assertEqual(cli.Fixture().singled(2), MACS[:2])


class FanOutTest(Case):
    def test_every_bulb_gets_the_setting(self):
        missed = self.set(kelvin=3000, brightness=50)
        self.assertEqual(missed, [])
        for bulb in self.net.bulbs.values():
            self.assertEqual((bulb["state"], bulb["temp"], bulb["dimming"]), (True, 3000, 50))
        self.assertEqual(len(self.net.packets("setPilot")), 7)

    def test_the_packets_leave_together(self):
        self.set(kelvin=3000, brightness=50)
        methods = [method for _, method, _ in self.net.sent]
        first = methods.index("setPilot")
        self.assertEqual(methods[first:first + 7], ["setPilot"] * 7)

    def test_a_lost_packet_is_sent_again(self):
        self.set(brightness=50)
        mac = MACS[3]
        self.net.sent = []
        self.net.drop[self.net.address(mac)] = 2
        plan = self.fixture.plan(dict(cli.DEFAULT_SETTING, on=True, kelvin=5000, brightness=60))
        self.assertEqual(self.fixture.send(plan), set(MACS))
        self.assertEqual(self.net.bulb(mac)["temp"], 5000)
        self.assertEqual(len(self.net.packets("setPilot", mac)), 3)
        self.assertEqual(len(self.net.packets("setPilot", MACS[0])), 1)
        # The question that comes before a setting is asked again too.
        self.net.drop[self.net.address(mac)] = 1
        self.assertEqual(self.set(kelvin=3000, brightness=60), [])
        self.assertEqual(self.net.bulb(mac)["temp"], 3000)

    def test_a_bulb_that_never_answers_is_reported(self):
        self.set(brightness=50)
        mac = MACS[2]
        self.net.down.add(self.net.address(mac))
        self.assertEqual(self.set(kelvin=5000, brightness=60), [mac])
        self.assertEqual(self.net.bulb(mac)["temp"], 2700)
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 5000)
        self.assertEqual(self.fixture.state()["pending"], [mac])

    def test_a_bulb_that_comes_back_is_sent_what_it_missed(self):
        self.set(brightness=50)
        mac = MACS[2]
        self.net.down.add(self.net.address(mac))
        self.set(kelvin=5000, brightness=60)
        self.net.down.clear()
        state, _ = self.fixture.reconcile()
        self.assertEqual(self.net.bulb(mac)["temp"], 5000)
        self.assertEqual(state["pending"], [])
        self.assertEqual(state["by"], "cli")  # not taken for a change made elsewhere

    def test_no_bulb_answering_is_a_failure(self):
        self.net.down = set(self.net.bulbs)
        with self.assertRaises(cli.Failure):
            self.set(brightness=50)

    def test_a_bulb_is_found_again_when_its_address_moves(self):
        self.set(brightness=50)
        first, last = self.net.address(MACS[0]), self.net.address(MACS[6])
        self.net.bulbs[first], self.net.bulbs[last] = self.net.bulbs[last], self.net.bulbs[first]
        self.net.bulbs["10.0.0.99"] = self.net.bulbs.pop(self.net.address(MACS[1]))
        self.fixture.reconcile()  # the command and the service look before they set
        self.assertEqual(self.set(kelvin=3500, brightness=3 / 7.0), [])
        self.assertEqual(sorted(self.net.lit()), [RING[0], RING[2], RING[4]])
        self.assertEqual(self.fixture.addresses[MACS[1]], "10.0.0.99")
        self.assertEqual(self.fixture.addresses[MACS[0]], last)

    def test_a_moved_bulb_that_misses_every_broadcast_is_still_found(self):
        self.set(brightness=50)
        self.net.deaf = True
        os.remove(cli.path("swept"))  # as if the last full search was long ago
        self.net.bulbs["10.0.0.99"] = self.net.bulbs.pop(self.net.address(MACS[1]))
        self.fixture.reconcile()  # the command and the service look before they set
        self.assertEqual(self.set(kelvin=3500, brightness=50), [])
        self.assertEqual(self.fixture.addresses[MACS[1]], "10.0.0.99")
        # Asking every address is not done again for a while.
        self.net.bulbs["10.0.0.98"] = self.net.bulbs.pop("10.0.0.99")
        self.fixture.reconcile()
        self.assertEqual(self.set(kelvin=3600, brightness=50), [MACS[1]])

    def test_local_network_refusal_is_named(self):
        self.set(brightness=50)

        def refuse(data, where):
            raise OSError(cli.errno.EHOSTUNREACH, "No route to host")
        self.net.sendto = refuse
        with self.assertRaises(cli.Failure) as raised:
            self.fixture.reconcile()
        self.assertIn("Local Network", str(raised.exception))


class BrightnessTest(Case):
    def test_whole_numbers_light_every_bulb_alike(self):
        for brightness in (1, 2, 37, 100):
            self.set(brightness=brightness)
            self.assertEqual(self.shown(), (7, [brightness]))

    def test_below_one_percent_switches_bulbs_off(self):
        self.set(brightness=0.4)
        self.assertEqual(self.shown(), (3, [1]))
        self.set(brightness=1)
        self.assertEqual(self.shown(), (7, [1]))

    def test_between_whole_numbers(self):
        self.set(brightness=12.5)
        self.assertEqual(self.shown(), (7, [12, 13]))
        self.assertEqual(sum(bulb["dimming"] for bulb in self.net.bulbs.values()), 88)
        self.assertEqual(self.setting()["brightness"], 12.5)  # the bulbs agree; nothing is re-read

    def test_bulbs_kept_off_still_get_the_look(self):
        self.set(kelvin=3000, brightness=0.3)
        for bulb in self.net.bulbs.values():
            self.assertEqual((bulb["temp"], bulb["dimming"]), (3000, 1))

    def test_colour_has_a_higher_floor(self):
        self.set(mode="colour", hue=0, saturation=100, brightness=5)
        self.assertEqual(self.shown(), (4, [10]))
        self.assertEqual(self.net.bulb(MACS[0])["r"], 255)
        self.assertEqual(self.setting()["brightness"], 5)
        self.set(mode="colour", hue=0, saturation=100, brightness=10)
        self.assertEqual(self.shown(), (7, [10]))

    def test_off_switches_every_bulb_off_and_on_brings_the_setting_back(self):
        self.set(brightness=0.4)
        self.fixture.apply(dict(self.setting(), on=False), "cli")
        self.assertEqual(self.net.lit(), [])
        self.fixture.apply(dict(self.setting(), on=True), "cli")
        self.assertEqual(self.shown(), (3, [1]))

    def test_how_it_is_described(self):
        setting = dict(cli.DEFAULT_SETTING, on=True, kelvin=2700)
        self.assertEqual(self.fixture.describe(dict(setting, brightness=0.43)), "white 2700K at 0.4% (3 of 7 bulbs lit)")
        self.assertEqual(self.fixture.describe(dict(setting, brightness=37.57)), "white 2700K at 37.6%")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=0.43)), "3 of 7 bulbs at 1%, the rest off")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=12.5)), "4 bulbs at 13%, 3 at 12%")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=12)), "all 7 bulbs at 12%")

    def test_the_command_line(self):
        self.assertEqual(cli.main(["white", "3000", "0.7"]), 0)
        self.assertEqual(self.shown(), (5, [1]))
        self.assertEqual(cli.main(["brightness", "12.5"]), 0)
        self.assertEqual(self.shown(), (7, [12, 13]))
        self.assertEqual(cli.main(["brightness", "100"]), 0)
        self.assertEqual(self.shown(), (7, [100]))
        self.assertEqual(cli.main(["off"]), 0)
        self.assertEqual(self.net.lit(), [])
        self.assertEqual(cli.main(["on"]), 0)
        self.assertEqual(self.shown(), (7, [100]))
        self.assertEqual(cli.main(["white", "9000", "5"]), 1)  # outside what the bulbs show
        self.assertEqual(cli.main(["status"]), 0)


class ElsewhereTest(Case):
    def test_a_change_made_elsewhere_is_noticed(self):
        self.set(kelvin=3000, brightness=50)
        for bulb in self.net.bulbs.values():
            bulb.update(temp=4200, dimming=100)
        state, _ = self.fixture.reconcile()
        self.assertEqual(state["by"], "elsewhere")
        self.assertEqual((state["setting"]["kelvin"], state["setting"]["brightness"]), (4200, 100))
        self.assertEqual(self.net.packets("setPilot")[7:], [])  # nothing was sent to undo it

    def test_one_bulb_changed_on_its_own_tile(self):
        self.set(kelvin=3000, brightness=50)
        self.net.bulb(MACS[4])["state"] = False
        state, _ = self.fixture.reconcile()
        self.assertEqual(state["by"], "elsewhere")
        self.assertTrue(state["setting"]["on"])
        self.assertLess(state["setting"]["brightness"], 50)
        self.assertEqual(len(self.net.lit()), 6)  # left as it was set

    def test_a_colour_set_elsewhere(self):
        self.set(brightness=50)
        for bulb in self.net.bulbs.values():
            del bulb["temp"]
            bulb.update(r=0, g=0, b=255, c=0, w=0, dimming=100)
        setting = self.setting()
        self.assertEqual((setting["mode"], setting["hue"], setting["saturation"]), ("colour", 240, 100))

    def test_switched_off_elsewhere_is_not_a_new_setting(self):
        self.set(brightness=50)
        before = self.fixture.state()
        for bulb in self.net.bulbs.values():
            bulb["state"] = False
        state, _ = self.fixture.reconcile()
        self.assertFalse(state["setting"]["on"])
        self.assertEqual((state["by"], state["set_at"]), (before["by"], before["set_at"]))
        for bulb in self.net.bulbs.values():
            bulb["state"] = True
        state, _ = self.fixture.reconcile()
        self.assertTrue(state["setting"]["on"])
        self.assertEqual((state["by"], state["set_at"]), (before["by"], before["set_at"]))

    def test_mixed_levels_are_not_taken_for_a_change(self):
        self.set(brightness=12.5)
        before = self.fixture.state()
        state, _ = self.fixture.reconcile()
        self.assertEqual(state, before)
        self.net.bulb(MACS[2])["dimming"] = 40
        state, _ = self.fixture.reconcile()
        self.assertEqual(state["by"], "elsewhere")
        self.assertEqual(state["setting"]["brightness"], round((88 - 12 + 40) / 7.0, 2))

    def test_power_back_at_the_wall_with_mixed_levels(self):
        self.set(brightness=12.5)
        for bulb in self.net.bulbs.values():
            bulb["state"] = False
        self.assertFalse(self.setting()["on"])
        for bulb in self.net.bulbs.values():
            bulb["state"] = True
        state, _ = self.fixture.reconcile()
        self.assertEqual((state["setting"]["on"], state["setting"]["brightness"], state["by"]), (True, 12.5, "cli"))

    def test_power_back_at_the_wall_keeps_the_low_level(self):
        self.set(brightness=0.43)
        before = self.fixture.state()
        for bulb in self.net.bulbs.values():
            bulb["state"] = True  # a bulb comes back on when its power returns
        state, _ = self.fixture.reconcile()
        self.assertEqual(sorted(self.net.lit()), [RING[0], RING[2], RING[4]])
        self.assertEqual(state["setting"]["brightness"], 0.43)
        self.assertEqual(state["by"], before["by"])

    def test_a_dry_look_changes_nothing(self):
        self.set(brightness=0.43)
        for bulb in self.net.bulbs.values():
            bulb["state"] = True
        sent = len(self.net.packets("setPilot"))
        before = self.fixture.state()
        self.fixture.reconcile(dry=True)
        self.assertEqual(len(self.net.packets("setPilot")), sent)
        self.assertEqual(self.fixture.state(), before)


class FollowTest(Case):
    def setUp(self):
        super().setUp()
        # The fixture as it might be found: set by hand some hours ago.
        self.set(kelvin=4000, brightness=80)
        self.age(300)

    def age(self, minutes):
        state = self.fixture.state()
        state["set_at"] = self.now - minutes * 60
        cli.write_json(cli.path("state.json"), state)

    def apply(self, minutes=0, target=None):
        now = self.sun(target, minutes) if target else self.now + minutes * 60
        if not target:
            os.utime(cli.SUNDIAL_STATE_FILE, (now, now))
        return cli.follow_apply(self.fixture, now)

    def test_day_night_and_between(self):
        follow = cli.follow_settings()
        self.assertEqual(cli.follow_target(DAY, follow, self.fixture), (6500, 100))
        self.assertEqual(cli.follow_target(NIGHT, follow, self.fixture), (2700, 1))
        kelvin, brightness = cli.follow_target(DUSK, follow, self.fixture)
        self.assertEqual((kelvin, brightness), (4100, 25.75))  # not rounded to a whole number
        halfway = {"sun_elevation": -3.0, "daylight": 0.0, "kelvin": 3400}
        self.assertEqual(cli.follow_target(halfway, follow, self.fixture), (3000, 1))  # 3050 rounds to even

    def test_colour_stays_inside_the_bulbs_range(self):
        follow = dict(cli.follow_settings(), night_kelvin=1800)
        self.assertEqual(cli.follow_target(NIGHT, follow, self.fixture)[0], 2200)
        hot = {"sun_elevation": 40.0, "daylight": 1.0, "kelvin": 9000}
        self.assertEqual(cli.follow_target(hot, follow, self.fixture)[0], 6500)

    def test_it_sets_the_fixture_and_logs_one_line(self):
        line = self.apply()
        self.assertIn("white 6500K at 100%", line)
        self.assertEqual(self.shown(), (7, [100]))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 6500)
        self.assertIsNone(self.apply(1))  # nothing changed, nothing said
        self.assertEqual(len(self.net.packets("setPilot")), 14)

    def test_night_is_every_bulb_at_its_lowest(self):
        line = self.apply(target=NIGHT)
        self.assertTrue(line.endswith("white 2700K at 1%"), line)
        self.assertEqual(self.shown(), (7, [1]))

    def test_a_night_level_below_one_percent(self):
        cli.write_json(cli.path("follow.json"), {"night_brightness": 0.4})
        line = self.apply(target=NIGHT)
        self.assertIn("white 2700K at 0.4% (3 of 7 bulbs lit)", line)
        self.assertEqual(self.shown(), (3, [1]))

    def test_dusk_moves_one_bulb_at_a_time(self):
        self.apply()
        sun = dict(DUSK)
        self.assertIn("white 4100K at 25.8%", self.apply(1, sun))  # 25.75: 180 units
        self.assertEqual(sum(bulb["dimming"] for bulb in self.net.bulbs.values()), 180)
        sent = len(self.net.packets("setPilot"))
        sun["daylight"] = 0.2501  # less than one bulb's step: nothing to send
        self.assertIsNone(self.apply(2, sun))
        self.assertEqual(len(self.net.packets("setPilot")), sent)
        sun["daylight"] = 0.2485  # one bulb's step down
        self.assertIn("white 4100K at 25.6%", self.apply(3, sun))
        self.assertEqual(sum(bulb["dimming"] for bulb in self.net.bulbs.values()), 179)

    def test_it_follows_through_dusk(self):
        self.apply()
        self.assertIn("white 4100K at 25.8%", self.apply(1, DUSK))
        self.assertIn("white 2700K at 1%", self.apply(2, NIGHT))
        self.assertIsNone(self.apply(3, NIGHT))

    def test_a_recent_setting_made_elsewhere_is_held(self):
        self.age(10)
        line = self.apply()
        self.assertIn("following again at", line)
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 4000)
        self.assertIsNone(self.apply(60))            # still held, and quiet
        self.assertIn("white 6500K", self.apply(111))  # two hours after it was set

    def test_a_change_on_the_bulbs_starts_a_hold(self):
        self.apply()
        for bulb in self.net.bulbs.values():
            bulb.update(temp=3000, dimming=40)       # the WiZ app, say
        self.assertIn("set elsewhere", self.apply(1))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 3000)
        self.assertIsNone(self.apply(100))
        self.assertIn("white 6500K at 100%", self.apply(122))

    def test_a_change_from_the_command_starts_a_hold(self):
        self.apply()
        cli.main(["white", "3000", "40"])
        self.assertIn("set elsewhere", self.apply(1))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 3000)

    def test_it_does_nothing_while_the_fixture_is_off(self):
        self.apply()
        cli.main(["off"])
        sent = len(self.net.packets("setPilot"))
        self.assertIn("off", self.apply(1, NIGHT))
        self.assertIsNone(self.apply(2, NIGHT))
        self.assertEqual(len(self.net.packets("setPilot")), sent)
        self.assertEqual(self.net.lit(), [])
        # Switching it on is not a new setting, so it follows straight away.
        cli.main(["on"])
        self.assertIn("white 2700K at 1%", self.apply(3, NIGHT))

    def test_off_at_the_bulbs_is_seen_too(self):
        self.apply()
        for bulb in self.net.bulbs.values():
            bulb["state"] = False
        self.assertIn("off", self.apply(1, NIGHT))
        self.assertEqual(self.net.lit(), [])

    def test_it_never_switches_off_at_night(self):
        self.apply(target=NIGHT)
        self.assertTrue(self.net.lit())
        self.assertTrue(self.fixture.state()["setting"]["on"])

    def test_power_back_at_the_wall_is_not_a_hold(self):
        cli.write_json(cli.path("follow.json"), {"night_brightness": 0.7})
        self.apply(target=NIGHT)
        for bulb in self.net.bulbs.values():
            bulb["state"] = True
        self.assertIsNone(self.apply(1, NIGHT))
        self.assertEqual(self.shown(), (5, [1]))
        self.assertEqual(self.fixture.state()["by"], "follow")

    def test_an_outage_is_one_line(self):
        self.apply()
        self.net.down = set(self.net.bulbs)
        self.assertIn("cannot follow the sun", self.apply(1, NIGHT))
        self.assertIsNone(self.apply(2, NIGHT))
        self.assertIsNone(self.apply(3, NIGHT))
        self.net.down = set()
        self.assertIn("white 2700K at 1%", self.apply(4, NIGHT))

    def test_stale_sundial_leaves_the_fixture_alone(self):
        self.apply()
        sent = len(self.net.packets("setPilot"))
        cli.write_json(cli.SUNDIAL_STATE_FILE, {"target": NIGHT})
        os.utime(cli.SUNDIAL_STATE_FILE, (self.now - 3600, self.now - 3600))
        self.assertIn("sundial last ran", cli.follow_apply(self.fixture, self.now))
        self.assertEqual(len(self.net.packets("setPilot")), sent)

    def test_a_bulb_out_of_reach_is_named_and_caught_up(self):
        mac = MACS[1]
        self.net.down.add(self.net.address(mac))
        self.assertIn("not answering: " + mac, self.apply())
        self.net.down.clear()
        self.assertIsNone(self.apply(1))
        self.assertEqual(self.net.bulb(mac)["temp"], 6500)

    def test_follow_off_and_on(self):
        self.age(10)
        cli.main(["follow", "off"])
        self.assertIn("switched off", self.apply())
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 4000)
        cli.main(["follow", "on"])  # ends the hold as well
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 6500)
        cli.main(["follow", "status"])

    def test_settings_are_read(self):
        cli.write_json(cli.path("follow.json"), {"night_brightness": 2, "night_kelvin": 2200, "day_brightness": "x"})
        follow = cli.follow_settings()
        self.assertEqual((follow["night_brightness"], follow["night_kelvin"], follow["day_brightness"]), (2, 2200, 100))
        self.apply(target=NIGHT)
        self.assertEqual(self.shown(), (7, [2]))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 2200)
        self.assertEqual(cli.FOLLOW_DEFAULTS["night_brightness"], 1)


class HomeKitTest(Case):
    def setUp(self):
        super().setUp()
        self.set(kelvin=2700, brightness=5)
        self.link = homekit.Link(self.fixture)

    def write(self, **values):
        self.link.apply_home(values)
        self.link.send_wanted()

    def test_what_home_is_shown(self):
        values = homekit.home_values(self.link.snapshot(), self.fixture.settings)
        self.assertEqual((values["On"], values["Brightness"], values["ColorTemperature"]), (True, 5, 370))
        self.assertTrue(20 <= values["Hue"] <= 40 and values["Saturation"] > 30)  # a warm white
        colour = dict(self.link.snapshot(), mode="colour", hue=200, saturation=60)
        values = homekit.home_values(colour, self.fixture.settings)
        self.assertEqual((values["Hue"], values["Saturation"]), (200, 60))

    def test_the_temperature_range_is_the_bulbs(self):
        self.assertEqual(homekit.mired_range(self.fixture.settings), (154, 454))
        self.assertEqual(self.fixture.kelvin(1e6 / 154), 6500)
        self.assertEqual(self.fixture.kelvin(1e6 / 454), 2200)

    def test_brightness_from_home(self):
        for brightness in (50, 2, 1):
            self.write(Brightness=brightness)
            self.assertEqual(self.shown(), (7, [brightness]))  # every bulb lit, all alike
        self.assertEqual(self.fixture.state()["by"], "home")

    def test_a_fraction_is_shown_rounded_and_survives_home_writing_it_back(self):
        for brightness, shown in ((0.43, 1), (12.5, 12), (37.57, 38)):
            self.set(brightness=brightness)
            self.assertTrue(self.link.poll())
            values = homekit.home_values(self.link.snapshot(), self.fixture.settings)
            self.assertEqual(values["Brightness"], shown)
            self.write(On=True, Brightness=shown)  # Home repeating what it was shown
            self.assertEqual(self.link.snapshot()["brightness"], brightness)
        self.write(Brightness=39)
        self.assertEqual(self.shown(), (7, [39]))

    def test_temperature_from_home_wins_over_the_hue_sent_with_it(self):
        self.write(ColorTemperature=250, Hue=30, Saturation=20)
        setting = self.link.snapshot()
        self.assertEqual((setting["mode"], setting["kelvin"]), ("white", 4000))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 4000)

    def test_colour_from_home(self):
        self.write(Hue=120, Saturation=100, Brightness=50)
        bulb = self.net.bulb(MACS[0])
        self.assertEqual((bulb["r"], bulb["g"], bulb["b"]), (0, 255, 0))
        self.assertNotIn("temp", bulb)
        self.assertFalse(self.link.poll())  # the bulbs agree; nothing to tell Home

    def test_off_and_on_from_home(self):
        self.write(On=False)
        self.assertEqual(self.net.lit(), [])
        self.write(On=True)
        self.assertEqual(self.shown(), (7, [5]))
        self.assertEqual(self.fixture.state()["by"], "cli")  # power is not a new setting

    def test_brightness_zero_is_never_sent(self):
        self.write(Brightness=0, On=False)
        self.assertEqual(self.link.snapshot()["brightness"], 1)

    def test_a_burst_sends_only_the_newest(self):
        sent = len(self.net.packets("setPilot"))
        for brightness in (20, 40, 60, 80):
            self.link.apply_home({"Brightness": brightness})
        self.link.send_wanted()
        self.assertEqual(len(self.net.packets("setPilot")), sent + 7)
        self.assertEqual(self.setting()["brightness"], 80)

    def test_a_change_made_elsewhere_reaches_home(self):
        self.assertFalse(self.link.poll())
        cli.main(["white", "5000", "60"])
        self.assertTrue(self.link.poll())
        self.assertEqual(self.link.snapshot()["kelvin"], 5000)
        self.assertTrue(self.link.reachable)

    def test_no_response_when_no_bulb_answers(self):
        self.link.poll()
        self.net.down = set(self.net.bulbs)
        self.link.poll()
        self.assertFalse(self.link.reachable)
        self.net.down = {self.net.address(MACS[0])}
        self.link.poll()
        self.assertTrue(self.link.reachable)  # six bulbs are enough

    def test_it_restarts_once_macos_allows_the_local_network(self):
        send, killed = self.net.sendto, []

        def refuse(data, where):
            raise OSError(cli.errno.EHOSTUNREACH, "No route to host")
        saved = homekit.os.kill
        self.addCleanup(lambda: setattr(homekit.os, "kill", saved))
        homekit.os.kill = lambda pid, sig: killed.append(sig)
        self.link.poll()
        self.assertEqual(killed, [])  # an ordinary start does not restart
        self.net.sendto = refuse
        self.link.poll()
        self.assertFalse(self.link.reachable)
        self.assertEqual(killed, [])
        self.net.sendto = send
        self.link.poll()
        self.assertEqual(killed, [homekit.signal.SIGTERM])

    def test_home_wins_over_a_poll_that_started_first(self):
        self.link.apply_home({"Brightness": 90})
        self.assertFalse(self.link.poll())
        self.assertEqual(self.link.snapshot()["brightness"], 90)

    def test_pairing_files_are_private(self):
        pairing = homekit.load_or_create_pairing()
        self.assertRegex(pairing["setup_code"], r"^\d{3}-\d{2}-\d{3}$")
        self.assertEqual(os.stat(homekit.pairing_file()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(homekit.data_dir()).st_mode & 0o777, 0o700)
        self.assertEqual(homekit.load_or_create_pairing(), pairing)


if __name__ == "__main__":
    unittest.main()
