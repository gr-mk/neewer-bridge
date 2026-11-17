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
        if method == "getUserConfig":
            return {"method": method, "env": "", "result": {"fadeIn": 450, "fadeOut": 500}}
        if method != "setPilot":
            # As the real bulbs answer setUserConfig: they will not take it.
            return {"method": method, "env": "", "error": {"code": -32600, "message": "Invalid Request"}}
        colour = any(key in values for key in "rgb")
        if "temp" in values:
            for key in "rgbcw":
                bulb.pop(key, None)
            bulb["temp"] = values["temp"]
        elif colour:
            bulb.pop("temp", None)
            bulb.update({key: values.get(key, 0) for key in "rgb"}, c=0, w=0)
        elif "c" in values or "w" in values:
            bulb.pop("temp", None)  # the white LEDs driven directly
            bulb.update(r=0, g=0, b=0, c=values.get("c", 0), w=values.get("w", 0))
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
            "APP_DIR", "SUNDIAL_STATE_FILE", "SUNDIAL_CONFIG_FILE", "open_socket", "own_address", "WAITS",
            "SEARCH_WAIT", "pause", "clock")}
        self.addCleanup(lambda: [setattr(cli, name, value) for name, value in saved.items()])
        cli.APP_DIR = self.folder.name
        cli.SUNDIAL_STATE_FILE = os.path.join(self.folder.name, "sundial.json")
        # No sundial settings: no bedtime, so the other tests pass at any hour.
        cli.SUNDIAL_CONFIG_FILE = os.path.join(self.folder.name, "sundial.ini")
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
        self.assertEqual([cli.percent(b) for b in (0.4, 0.03, 0.003, 1.0, 12.5, 37.57, 100)],
                         ["0.4%", "0.03%", "0.003%", "1%", "12.5%", "37.6%", "100%"])


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
        self.assertEqual(self.fixture.describe(dict(setting, brightness=0.4)), "white 2700K at 0.4% (3 of 7 bulbs lit)")
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
        self.assertEqual(cli.main(["fade"]), 0)
        self.assertEqual(cli.main(["fade", "100"]), 1)  # these bulbs refuse; it is checked by reading back


class RawTest(Case):
    """White below 1% on bulbs whose white LEDs can be driven directly."""

    def setUp(self):
        super().setUp()
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved["raw_calibration"] = [{"kelvin": 2700, "percent": 1, "c": 0, "w": 42, "dimming": 10}]
        cli.write_json(cli.path("fixture.json"), saved)
        self.fixture = cli.Fixture()

    def warm(self):
        """{mac: warm LED level} for the bulbs that are lit; all must be in raw mode."""
        for bulb in self.net.bulbs.values():
            self.assertNotIn("temp", bulb)
            self.assertEqual((bulb["dimming"], bulb["c"], bulb["r"]), (10, 0, 0))
        return {bulb["mac"]: bulb["w"] for bulb in self.net.bulbs.values() if bulb["state"]}

    def test_every_bulb_stays_lit_below_one_percent(self):
        self.set(brightness=0.5)
        self.assertEqual(self.warm(), {mac: 21 for mac in MACS})
        self.set(brightness=0.1)  # 4.2 a bulb: 29 sevenths, so one bulb a step higher
        self.assertEqual(self.warm(), dict({mac: 4 for mac in MACS}, **{MACS[0]: 5}))
        self.set(brightness=1 / 42.0)
        self.assertEqual(self.warm(), {mac: 1 for mac in MACS})

    def test_the_extra_step_follows_the_patterns(self):
        self.set(brightness=(21 * 7 + 3) / 294.0)
        self.assertEqual(self.warm(), dict({mac: 21 for mac in MACS}, **{mac: 22 for mac in (RING[0], RING[2], RING[4])}))

    def test_one_percent_and_up_is_ordinary_white(self):
        for brightness in (1, 0.999, 12.5):
            self.set(brightness=brightness)
            for bulb in self.net.bulbs.values():
                self.assertEqual(bulb["temp"], 2700)
                self.assertNotIn("w", bulb)
        self.set(brightness=0.999)
        self.assertEqual(self.shown(), (7, [1]))

    def test_below_every_bulb_at_level_one_lights_fewer(self):
        self.set(brightness=3 / 294.0)
        self.assertEqual(self.warm(), {RING[0]: 1, RING[2]: 1, RING[4]: 1})
        self.set(brightness=0.001)
        self.assertEqual(self.warm(), {MACS[0]: 1})  # the least there is: one bulb at level 1
        for bulb in self.net.bulbs.values():
            self.assertEqual(bulb["w"], 1)  # bulbs kept off are ready at level 1

    def test_the_light_rises_one_step_at_a_time(self):
        totals = []
        for units in range(1, 294):
            bulbs = self.fixture.plan(dict(cli.DEFAULT_SETTING, on=True, brightness=units / 294.0))
            totals.append(sum(bulb["look"]["w"] for bulb in bulbs.values() if bulb["on"]))
        self.assertEqual(totals, list(range(1, 294)))

    def test_other_colour_temperatures_share_the_two_white_leds(self):
        looks = {kelvin: self.fixture.raw_look(20, {"share": self.fixture.cold_share(kelvin), "dimming": 10})
                 for kelvin in (2200, 2700, 4000, 6500)}
        self.assertEqual(looks[2200], {"w": 20, "dimming": 10})
        self.assertEqual(looks[2700], {"w": 20, "dimming": 10})
        self.assertEqual(looks[6500], {"c": 20, "dimming": 10})
        self.assertEqual(looks[4000], {"c": 11, "w": 9, "dimming": 10})  # the same total
        self.set(kelvin=4000, brightness=0.5)
        bulb = self.net.bulb(MACS[0])
        self.assertEqual((bulb["c"], bulb["w"]), (12, 9))

    def test_how_it_is_described(self):
        setting = dict(cli.DEFAULT_SETTING, on=True, kelvin=2700)
        self.assertEqual(self.fixture.describe(dict(setting, brightness=0.4)), "white 2700K at 0.4% (raw warm 16.9)")
        self.assertEqual(self.fixture.describe(dict(setting, brightness=0.01)),
                         "white 2700K at 0.01% (raw warm 1, 3 of 7 bulbs lit)")
        self.assertEqual(self.fixture.describe(dict(setting, brightness=0.5, kelvin=4000)),
                         "white 4000K at 0.5% (raw white 21)")
        self.assertEqual(self.fixture.describe(dict(setting, brightness=1)), "white 2700K at 1%")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=0.4)), "6 bulbs at raw 17, 1 at raw 16")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=0.5)), "all 7 bulbs at raw 21")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=0.1)), "1 bulb at raw 5, 6 at raw 4")
        self.assertEqual(self.fixture.describe_bulbs(dict(setting, brightness=0.01)), "3 of 7 bulbs at raw 1, the rest off")

    def test_raw_bulbs_are_not_taken_for_a_change(self):
        self.set(brightness=0.4)
        before = self.fixture.state()
        state, _ = self.fixture.reconcile()
        self.assertEqual(state, before)
        self.assertEqual(state["setting"]["brightness"], 0.4)

    def test_a_raw_level_set_elsewhere_is_read(self):
        self.set(brightness=0.4)
        for bulb in self.net.bulbs.values():
            bulb["w"] = 21
        state, _ = self.fixture.reconcile()
        self.assertEqual((state["by"], state["setting"]["mode"], state["setting"]["brightness"]), ("elsewhere", "white", 0.5))
        self.assertEqual(self.net.packets("setPilot")[7:], [])

    def test_off_and_power_back_at_the_wall(self):
        self.set(brightness=0.01)
        for bulb in self.net.bulbs.values():
            bulb["state"] = False
        self.assertFalse(self.setting()["on"])
        for bulb in self.net.bulbs.values():
            bulb["state"] = True
        state, _ = self.fixture.reconcile()
        self.assertEqual((state["setting"]["on"], state["setting"]["brightness"], state["by"]), (True, 0.01, "cli"))
        self.assertEqual(sorted(self.warm()), sorted([RING[0], RING[2], RING[4]]))

    def test_home_shows_one_percent(self):
        self.set(brightness=0.03)
        link = homekit.Link(self.fixture)
        values = homekit.home_values(link.snapshot(), self.fixture.settings)
        self.assertEqual((values["On"], values["Brightness"], values["ColorTemperature"]), (True, 1, 370))
        link.apply_home({"On": True, "Brightness": 1})  # Home repeating what it was shown
        link.send_wanted()
        self.assertEqual(self.setting()["brightness"], 0.03)
        link.apply_home({"Brightness": 2})
        link.send_wanted()
        self.assertEqual(self.shown(), (7, [2]))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 2700)

    def test_the_follower_ramps_below_one_percent(self):
        cli.write_json(cli.path("follow.json"), {"night_brightness": 0.1})
        self.set(kelvin=4000, brightness=80)
        self.fixture.claim("follow")
        self.assertIn("white 2700K at 0.1% (raw warm 4.1)", cli.follow_apply(self.fixture, self.sun(NIGHT)))
        self.assertEqual(sorted(self.warm().values()), [4, 4, 4, 4, 4, 4, 5])
        dawn = {"sun_elevation": -5.9, "daylight": 0.005, "kelvin": 3400}
        self.assertIn("white 2700K at 0.6% (raw warm 25", cli.follow_apply(self.fixture, self.sun(dawn, 1)))
        self.assertIsNone(cli.follow_apply(self.fixture, self.sun(dawn, 2)))

    def test_sweep_crosses_one_percent(self):
        self.assertEqual(cli.main(["sweep", "0.9", "1.2", "--seconds", "0", "--kelvin", "2700"]), 0)
        self.assertEqual(self.shown(), (7, [1, 2]))
        self.assertEqual(cli.main(["sweep", "1.2", "0.5", "--seconds", "0.5"]), 0)
        self.assertEqual(self.warm(), {mac: 21 for mac in MACS})


class RawModeTest(Case):
    """white_mode "raw": the white LEDs are driven directly at every brightness."""

    POINT = {"kelvin": 2700, "percent": 1, "c": 0, "w": 42, "dimming": 10}

    def configure(self, *points, **more):
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved.update(white_mode="raw", raw_calibration=[self.POINT] + list(points), **more)
        cli.write_json(cli.path("fixture.json"), saved)
        self.fixture = cli.Fixture()

    def raw(self, brightness, kelvin=2700):
        return self.fixture.raw(dict(cli.DEFAULT_SETTING, on=True, kelvin=kelvin, brightness=brightness))

    def levels(self):
        return sorted({(bulb.get("c", 0), bulb["w"], bulb["dimming"]) for bulb in self.net.bulbs.values()
                       if bulb["state"] and "temp" not in bulb})

    def test_hybrid_is_the_default(self):
        self.configure()
        self.assertEqual(cli.settings()["white_mode"], "raw")
        cli.write_json(cli.path("fixture.json"), {"bulbs": MACS, "white_mode": "sideways"})
        self.assertEqual(cli.settings()["white_mode"], "hybrid")

    def test_no_ordinary_white_at_any_brightness(self):
        self.configure()
        for brightness, expected in ((1, (0, 42, 10)), (2, (0, 84, 10)), (0.5, (0, 21, 10)), (10, (0, 247, 17)),
                                     (100, (0, 255, 100))):
            self.set(brightness=brightness)
            self.assertEqual(self.levels(), [expected], brightness)
            self.assertEqual(len(self.net.lit()), 7)

    def test_only_the_measured_point_is_covered(self):
        self.configure()
        self.assertTrue(self.raw(1)["covered"])
        self.assertTrue(self.raw(0.3)["covered"])
        self.assertFalse(self.raw(2)["covered"])
        self.assertFalse(self.raw(1, kelvin=4000)["covered"])
        self.assertFalse(self.raw(0.3, kelvin=4000)["covered"])

    def test_a_second_point_is_one_line(self):
        self.configure({"kelvin": 2700, "percent": 50, "w": 200, "dimming": 60})
        self.assertEqual(self.fixture.calibrated(2700, 50), (200.0, 60, True))
        level, dimming, covered = self.fixture.calibrated(2700, 25.5)  # half way between the two
        self.assertEqual((round(level * dimming), dimming, covered), (6210, 35, True))
        level, dimming, covered = self.fixture.calibrated(2700, 75)    # past the last: in proportion to it
        self.assertEqual((round(level * dimming), covered), (18000, False))
        self.set(brightness=50)
        self.assertEqual(self.levels(), [(0, 200, 60)])

    def test_a_point_decides_the_colour_mix_at_its_colour_temperature(self):
        self.configure({"kelvin": 4000, "percent": 1, "c": 30, "w": 10, "dimming": 10})
        self.assertEqual(self.fixture.cold_share(4000), 0.75)
        self.assertTrue(self.raw(1, kelvin=4000)["covered"])
        self.set(kelvin=4000, brightness=1)
        self.assertEqual(self.levels(), [(30, 10, 10)])

    def test_between_levels_one_bulb_at_a_time(self):
        self.configure()
        self.set(brightness=(50 * 7 + 2) / 294.0)
        levels = {bulb["mac"]: bulb["w"] for bulb in self.net.bulbs.values()}
        self.assertEqual(levels, dict({mac: 50 for mac in MACS}, **{RING[0]: 51, RING[3]: 51}))

    def test_bad_points_are_ignored(self):
        self.configure("nonsense", {"kelvin": 2700}, {"kelvin": 2700, "percent": 5, "w": 900}, {"kelvin": 2700, "percent": 0, "w": 9})
        self.assertEqual(cli.settings()["raw_calibration"], [self.POINT])

    def test_without_a_one_percent_point_white_is_ordinary(self):
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved.update(white_mode="raw", raw_calibration=[{"kelvin": 2700, "percent": 50, "w": 200, "dimming": 60}])
        cli.write_json(cli.path("fixture.json"), saved)
        self.fixture = cli.Fixture()
        self.set(brightness=50)
        self.assertEqual(self.shown(), (7, [50]))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 2700)

    def test_home_and_changes_elsewhere_still_work(self):
        self.configure()
        self.set(brightness=3)
        link = homekit.Link(self.fixture)
        self.assertEqual(homekit.home_values(link.snapshot(), self.fixture.settings)["Brightness"], 3)
        self.assertFalse(link.poll())
        for bulb in self.net.bulbs.values():
            bulb["w"] = 84
        self.assertTrue(link.poll())
        self.assertEqual(link.snapshot()["brightness"], 2)


class CompareTest(Case):
    def setUp(self):
        super().setUp()
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved.update(ring_names=["north", "northeast", "southeast", "south", "southwest", "northwest"])
        cli.write_json(cli.path("fixture.json"), saved)
        self.set(kelvin=2700, brightness=1)
        self.fixture.claim("follow")

    def test_bulbs_by_name(self):
        for name, mac in (("centre", MACS[0]), ("north", RING[0]), ("South", RING[3]), ("ring 5", RING[5]),
                          ("2", RING[2]), (MACS[4], MACS[4])):
            self.assertEqual(cli.named_bulb(self.fixture, name), mac)
        with self.assertRaises(cli.Failure):
            cli.named_bulb(self.fixture, "upstairs")

    def test_one_bulb_is_set_and_the_rest_are_left(self):
        self.assertEqual(cli.main(["compare", "--bulb", "south", "--raw-w", "120", "--raw-dimming", "40"]), 0)
        bulb = self.net.bulb(RING[3])
        self.assertEqual((bulb["w"], bulb["c"], bulb["dimming"], bulb["state"]), (120, 0, 40, True))
        self.assertNotIn("temp", bulb)
        for mac in MACS:
            if mac != RING[3]:
                self.assertEqual((self.net.bulb(mac)["temp"], self.net.bulb(mac)["dimming"]), (2700, 1))

    def test_it_is_dim_unless_asked(self):
        self.assertEqual(cli.main(["compare", "--bulb", "centre"]), 0)
        bulb = self.net.bulb(MACS[0])
        self.assertEqual((bulb["w"], bulb["c"], bulb["dimming"]), (1, 0, 10))

    def test_restore_puts_it_back_and_leaves_no_hold(self):
        before = self.fixture.state()
        cli.main(["compare", "--bulb", "centre", "--raw-w", "42"])
        self.fixture.reconcile()  # the service notices the bulb, as it would
        self.assertEqual(self.fixture.state()["by"], "elsewhere")
        cli.main(["compare", "--bulb", "north", "--raw-w", "30"])
        self.assertEqual(cli.main(["compare", "--restore"]), 0)
        self.assertEqual(self.shown(), (7, [1]))
        for bulb in self.net.bulbs.values():
            self.assertEqual(bulb["temp"], 2700)
        after = self.fixture.state()
        self.assertEqual((after["by"], after["set_at"], after["setting"]), (before["by"], before["set_at"], before["setting"]))
        self.assertEqual(cli.main(["compare", "--restore"]), 1)  # nothing left to restore
        sent = len(self.net.packets("setPilot"))
        self.assertIn("already at white 2700K at 1%", cli.follow_apply(self.fixture, self.sun(NIGHT)))  # no hold
        self.assertEqual(len(self.net.packets("setPilot")), sent)


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
        self.assertEqual(state["setting"]["brightness"], round((88 - 12 + 40) / 7.0, 3))

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


class BedtimeTest(Case):
    """The bedtime rule, with sundial saying bedtime 23:00 and wake 07:00."""

    def setUp(self):
        super().setUp()
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved["raw_calibration"] = [{"kelvin": 2700, "percent": 1, "c": 0, "w": 42, "dimming": 10}]
        cli.write_json(cli.path("fixture.json"), saved)
        self.fixture = cli.Fixture()
        with open(cli.SUNDIAL_CONFIG_FILE, "w") as handle:
            handle.write("[sleep]\nwake = 07:00\nbedtime = 23:00   # a comment\n")
        self.ticks, self.during = 0.0, None
        cli.pause, cli.clock = self.pause, lambda: self.ticks
        self.midnight = time.mktime(time.localtime(self.now)[:3] + (0, 0, 0, 0, 0, -1))
        self.sky = NIGHT
        # An evening: the follower has the fixture at the night level.
        self.set(kelvin=4000, brightness=80)
        self.fixture.claim("follow")
        self.assertIn("white 2700K at 1%", self.apply(22, 0))
        self.net.sent = []

    def pause(self, seconds):
        self.ticks += seconds
        if self.during:
            self.during()

    def apply(self, hour, minute, day=0):
        self.when = self.midnight + day * 86400 + hour * 3600 + minute * 60
        cli.write_json(cli.SUNDIAL_STATE_FILE, {"target": self.sky})
        os.utime(cli.SUNDIAL_STATE_FILE, (self.when, self.when))
        return cli.follow_apply(self.fixture, self.when)

    def status(self):
        return cli.bedtime_outlook(self.fixture, cli.follow_settings(), cli.read_json(cli.path("follow-state.json"), {}),
                                   self.fixture.state()["setting"]["on"], self.when)

    def switch(self, on):
        """The whole fixture switched by hand, at the bulbs."""
        for bulb in self.net.bulbs.values():
            bulb["state"] = on

    def sets(self):
        return len(self.net.packets("setPilot"))

    def test_times_come_from_sundial(self):
        self.assertEqual(cli.sleep_times(cli.follow_settings()), (23 * 60, 7 * 60))
        self.assertEqual(self.status(), "off at 23:00, on again at 06:30")

    def test_fade_and_off_at_bedtime(self):
        totals = []

        def watch():
            lit = [bulb for bulb in self.net.bulbs.values() if bulb["state"]]
            self.assertTrue(all("w" in bulb or bulb["dimming"] == 1 for bulb in lit))
            totals.append(sum(bulb.get("w", 42) for bulb in lit))
        self.during = watch
        line = self.apply(23, 0)
        self.assertEqual(line, "bedtime: faded out and switched off; off for the night; on at 06:30")
        self.assertEqual(self.net.lit(), [])
        self.assertAlmostEqual(self.ticks, 300, delta=2)           # five minutes
        self.assertEqual(totals, sorted(totals, reverse=True))     # only ever dimmer
        self.assertEqual((totals[0], totals[-1]), (294, 1))        # down through the raw levels to one bulb
        self.assertGreater(len(set(totals)), 100)                  # in small steps
        # The record and the bulbs keep the setting from before the fade.
        setting = self.fixture.state()["setting"]
        self.assertEqual((setting["on"], setting["brightness"], setting["kelvin"]), (False, 1, 2700))
        for bulb in self.net.bulbs.values():
            self.assertEqual((bulb["temp"], bulb["dimming"]), (2700, 1))
        self.assertEqual(self.status(), "off for the night; on at 06:30")
        sent = self.sets()
        for minute in (1, 30, 59):
            self.assertIsNone(self.apply(23, minute))
        self.assertEqual(self.sets(), sent)

    def test_a_bright_fixture_fades_all_the_way(self):
        self.set(kelvin=3000, brightness=60)
        self.fixture.claim("follow")
        self.sky = DAY
        self.apply(22, 30)
        self.assertEqual(self.shown(), (7, [100]))
        self.assertIn("faded out and switched off", self.apply(23, 0))
        self.assertEqual(self.net.lit(), [])
        self.assertEqual(self.fixture.state()["setting"]["brightness"], 100)

    def test_already_off_at_bedtime(self):
        self.switch(False)
        self.assertEqual(self.apply(23, 0), "off by hand; left alone tonight")
        self.net.sent = []
        self.apply(6, 30, day=1)
        self.apply(9, 0, day=1)
        self.assertEqual(self.sets(), 0)  # he switched it off; it stays off
        self.assertEqual(self.net.lit(), [])

    def test_switched_back_on_in_the_night_is_left_alone(self):
        self.apply(23, 0)
        self.switch(True)
        self.assertEqual(self.apply(1, 10, day=1), "on by hand since 01:10; left alone tonight")
        self.assertEqual(self.status(), "on by hand since 01:10; left alone tonight")
        sent = self.sets()
        for hour in (2, 4, 6):  # well past the two-hour hold: still not switched off again
            self.assertIsNone(self.apply(hour, 0, day=1))
        self.assertEqual(self.sets(), sent)
        self.assertEqual(len(self.net.lit()), 7)
        self.sky = DUSK
        self.assertIn("white 4100K at 25.8%", self.apply(6, 30, day=1))  # in the morning it follows again

    def test_set_from_home_in_the_night_is_left_alone(self):
        self.apply(23, 0)
        self.fixture.apply(dict(self.fixture.state()["setting"], on=True, brightness=60), "home")
        self.assertIn("on by hand since 00:30", self.apply(0, 30, day=1))
        sent = self.sets()
        self.assertIsNone(self.apply(3, 0, day=1))
        self.assertEqual(self.sets(), sent)
        self.assertEqual(self.shown(), (7, [60]))

    def test_switched_off_by_hand_stays_off_in_the_morning(self):
        self.apply(23, 0)
        self.switch(True)
        self.apply(1, 0, day=1)
        self.switch(False)
        self.assertEqual(self.apply(2, 0, day=1), "off by hand; left alone tonight")
        sent = self.sets()
        for hour, minute in ((6, 30), (7, 0), (12, 0)):
            self.apply(hour, minute, day=1)
        self.assertEqual(self.sets(), sent)
        self.assertEqual(self.net.lit(), [])

    def test_on_again_in_the_morning_and_following(self):
        self.apply(23, 0)
        sent = self.sets()
        self.assertIsNone(self.apply(6, 29, day=1))
        self.assertEqual(self.sets(), sent)
        self.sky = {"sun_elevation": -5.0, "daylight": 0.04, "kelvin": 3400}
        self.assertEqual(self.apply(6, 30, day=1), "morning: switched on at white 2800K at 5%")
        self.assertEqual(len(self.net.lit()), 7)   # every bulb back, as the plan has them
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 2800)
        self.sky = DUSK
        self.assertIn("white 4100K at 25.8%", self.apply(6, 50, day=1))  # no hold: it rises with the dawn
        self.assertEqual(self.status(), "off at 23:00, on again at 06:30")

    def test_morning_on_can_be_switched_off(self):
        cli.write_json(cli.path("follow.json"), {"morning_on": False})
        self.assertEqual(self.apply(23, 0), "bedtime: faded out and switched off; off for the night")
        sent = self.sets()
        self.apply(6, 30, day=1)
        self.apply(8, 0, day=1)
        self.assertEqual(self.sets(), sent)
        self.assertEqual(self.net.lit(), [])

    def test_own_bedtime_and_wake(self):
        cli.write_json(cli.path("follow.json"), {"bedtime": "21:30", "wake": "06:00", "morning_lead_minutes": 15,
                                                 "fade_minutes": 1})
        self.apply(21, 29, day=1)
        self.assertEqual(self.status(), "off at 21:30, on again at 05:45")
        self.assertEqual(len(self.net.lit()), 7)
        self.assertIn("on at 05:45", self.apply(21, 30, day=1))
        self.assertAlmostEqual(self.ticks, 60, delta=2)
        self.assertIsNone(self.apply(5, 44, day=2))
        self.assertIn("morning: switched on", self.apply(5, 45, day=2))

    def test_the_rule_can_be_switched_off(self):
        cli.write_json(cli.path("follow.json"), {"bedtime_rule": False})
        self.assertIsNone(self.apply(23, 0))
        self.assertEqual(self.status(), "the rule is switched off")
        self.sky = DUSK
        self.assertIn("white 4100K at 25.8%", self.apply(23, 5))  # still following, still on
        self.assertEqual(len(self.net.lit()), 7)

    def test_started_in_the_middle_of_the_night(self):
        os.remove(cli.path("follow-state.json"))
        self.assertEqual(self.apply(2, 45, day=1), "on by hand; left alone tonight")
        self.assertEqual(self.status(), "on by hand; left alone tonight")
        self.assertIsNone(self.apply(3, 0, day=1))
        self.assertEqual(self.sets(), 0)
        self.apply(9, 0, day=1)                       # day: following again
        self.assertEqual(self.status(), "off at 23:00, on again at 06:30")
        self.assertIn("faded out and switched off", self.apply(23, 0, day=1))  # the first bedtime it acts on

    def test_a_late_start_after_bedtime_leaves_the_night_alone(self):
        self.assertEqual(self.apply(23, 40), "on by hand; left alone tonight")  # the Mac was asleep at 23:00
        self.assertEqual(self.sets(), 0)

    def test_a_setting_made_during_the_fade_stops_it(self):
        def meddle():
            if self.ticks >= 120 and self.fixture.state()["by"] != "home":
                self.fixture.apply(dict(self.fixture.state()["setting"], brightness=50), "home")
        self.during = meddle
        self.assertIn("on by hand since 23:0", self.apply(23, 0))
        self.assertEqual(self.shown(), (7, [50]))
        sent = self.sets()
        self.assertIsNone(self.apply(23, 30))
        self.assertEqual(self.sets(), sent)

    def test_a_held_setting_is_still_switched_off_at_bedtime(self):
        self.fixture.apply(dict(self.fixture.state()["setting"], brightness=40, kelvin=3000), "home")
        state = self.fixture.state()
        state["set_at"] = self.midnight + 22 * 3600 + 25 * 60  # five minutes before the next run
        cli.write_json(cli.path("state.json"), state)
        self.assertIn("set elsewhere", self.apply(22, 30))
        self.assertIn("faded out and switched off", self.apply(23, 0))
        setting = self.fixture.state()["setting"]
        self.assertEqual((setting["on"], setting["brightness"], setting["kelvin"]), (False, 40, 3000))

    def test_without_raw_white_the_fade_ends_on_one_bulb(self):
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved["raw_calibration"] = []
        cli.write_json(cli.path("fixture.json"), saved)
        self.fixture = cli.Fixture()
        counts = []
        self.during = lambda: counts.append(len(self.net.lit()))
        self.assertIn("faded out and switched off", self.apply(23, 0))
        self.assertEqual((counts[0], counts[-1]), (7, 1))
        self.assertEqual(self.net.lit(), [])

    def test_no_bedtime_known(self):
        os.remove(cli.SUNDIAL_CONFIG_FILE)
        self.assertIsNone(self.apply(23, 0))
        self.assertIn("no bedtime known", self.status())
        self.assertEqual(len(self.net.lit()), 7)


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

    def burst(self, writes, every=0.05):
        """Home writing `writes` one after another, `every` seconds apart, as a drag does."""
        saved = (homekit.pause, homekit.clock)
        self.addCleanup(lambda: setattr(homekit, "pause", saved[0]) or setattr(homekit, "clock", saved[1]))
        state = {"now": 0.0, "next": every, "left": list(writes[1:])}

        def pause(seconds):
            state["now"] += seconds
            while state["left"] and state["now"] >= state["next"]:
                self.link.apply_home(state["left"].pop(0))
                state["next"] += every
        homekit.pause, homekit.clock = pause, lambda: state["now"]
        self.link.apply_home(writes[0])
        self.link.send_burst()
        return state["now"]

    def test_a_drag_is_streamed_and_then_confirmed(self):
        self.net.sent = []
        took = self.burst([{"Brightness": level} for level in range(10, 60, 2)], every=0.05)
        sent = [values["dimming"] for values in self.net.packets("setPilot", MACS[0])]
        # 25 writes in 1.2 seconds, sent ten times a second: about every other one, in order.
        self.assertTrue(10 <= len(sent) <= 16, sent)
        self.assertEqual(sent, sorted(sent))
        self.assertEqual(sent[-2:], [58, 58])                # the last one twice: streamed, then confirmed
        self.assertEqual(self.shown(), (7, [58]))
        self.assertAlmostEqual(took, 1.2 + homekit.BURST_QUIET_SECONDS, delta=0.25)
        state = self.fixture.state()
        self.assertEqual((state["setting"]["brightness"], state["by"], state["pending"]), (58, "home", []))
        self.assertFalse(self.link.unsent)

    def test_the_record_is_written_only_at_the_end_of_a_drag(self):
        seen = []
        stream = self.fixture.stream
        self.fixture.stream = lambda setting, sock: (seen.append(self.fixture.state()["setting"]["brightness"]),
                                                     stream(setting, sock))
        self.burst([{"Brightness": level} for level in (20, 30, 40)])
        self.assertEqual(set(seen), {5})
        self.assertEqual(self.fixture.state()["setting"]["brightness"], 40)

    def test_a_single_tap_is_sent_at_once_and_confirmed(self):
        self.net.sent = []
        self.burst([{"On": False}])
        self.assertEqual(self.net.lit(), [])
        self.assertEqual(len(self.net.packets("setPilot", MACS[0])), 2)
        self.assertFalse(self.fixture.state()["setting"]["on"])

    def test_a_drag_below_one_percent_streams_the_same_way(self):
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved["raw_calibration"] = [{"kelvin": 2700, "percent": 1, "c": 0, "w": 42, "dimming": 10}]
        cli.write_json(cli.path("fixture.json"), saved)
        self.set(kelvin=2700, brightness=0.5)
        self.link.poll()
        self.net.sent = []
        self.burst([{"ColorTemperature": 370}, {"Brightness": 3}, {"Brightness": 2}])
        self.assertEqual(self.shown(), (7, [2]))
        self.assertEqual(self.net.bulb(MACS[0])["temp"], 2700)

    def test_the_poll_keeps_out_of_a_drag(self):
        self.link.apply_home({"Brightness": 30})
        self.net.sent = []
        self.assertFalse(self.link.poll())
        self.assertEqual(self.net.sent, [])  # not even a question to the bulbs

    def test_a_bulb_that_misses_the_stream_is_caught_by_the_confirmed_send(self):
        self.net.drop[self.net.address(MACS[3])] = 3
        self.burst([{"Brightness": level} for level in (20, 30, 40)])
        self.assertEqual(self.shown(), (7, [40]))
        self.assertEqual(self.fixture.state()["pending"], [])

    def test_streaming_can_be_switched_off(self):
        saved = cli.read_json(cli.path("fixture.json"), {})
        saved["stream_rate"] = 0
        cli.write_json(cli.path("fixture.json"), saved)
        self.fixture.settings = cli.settings()
        self.net.sent = []
        self.burst([{"Brightness": 30}])
        self.assertEqual(len(self.net.packets("setPilot", MACS[0])), 1)
        self.assertEqual(self.shown(), (7, [30]))

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
        self.link.unsent = True  # as it is from Home's write until the send is done
        self.link.wanted = dict(self.link.wanted, brightness=90.0)
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
