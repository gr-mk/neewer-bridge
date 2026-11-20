#!/usr/bin/env python3
"""Presents the Neewer light to Apple Home as one colour light.

It runs as a child of the Neewer HomeKit launcher app and sends commands through
the local Neewer Bridge, using the same frame builder as the `neewer` command
(install.sh places a copy of that script next to this file as neewer_cli.py).

The light never reports its state, so Apple Home shows what was last sent, by
Home or by the `neewer` command. Changes made on the light's own dial are not seen.

A second tile, the Sunrise switch, runs `neewer sunrise`, so a Home automation
can start the ramp at a set time. It turns itself off when the ramp ends.

    neewer_homekit.py            run the service
    neewer_homekit.py --pairing  print the setup code and pairing state as JSON
    neewer_homekit.py --qr FILE  write the pairing QR code to FILE as SVG
"""

import colorsys
import json
import logging
import math
import os
import secrets
import signal
import string
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import neewer_cli as cli  # noqa: E402

VERSION = "1.1.0"
CLI_FILE = os.path.join(HERE, "neewer_cli.py")
DATA_DIR = os.path.join(cli.APP_DIR, "homekit-data")
KEYS_FILE = os.path.join(DATA_DIR, "accessory.state")
PAIRING_FILE = os.path.join(DATA_DIR, "pairing.json")
SETTINGS_FILE = os.path.join(cli.APP_DIR, "homekit.json")

DEFAULT_NAME = "Neewer CB200C"
DEFAULT_PORT = 51826
# The HomeKit specification rejects these setup codes as too guessable.
WEAK_CODES = {str(d) * 8 for d in range(10)} | {"12345678", "87654321"}

log = logging.getLogger("neewer-homekit")


# ---------------------------------------------------------------- conversions

# The light's profile (model, colour temperatures, curve, what it takes) is
# the command's: light.json's "profile". It is read when the service starts.

def mired_range(profile):
    """(coolest, warmest) in mireds, both inside what the light shows: 154-370 for 2700-6500K."""
    return int(math.ceil(1e6 / profile["kelvin_max"])), int(math.floor(1e6 / profile["kelvin_min"]))


def mired_to_kelvin(mired, profile):
    return cli.fit_kelvin(profile, 1e6 / mired)


def kelvin_to_mired(kelvin, profile):
    low, high = mired_range(profile)
    return max(low, min(high, int(round(1e6 / kelvin))))


# Approximate colour of a white point, so Home's colour wheel matches the light.
kelvin_to_hue_saturation = cli.white_hue_saturation


# ---------------------------------------------------------------- pairing record

def private_write(path, value):
    """Writes JSON readable only by this user: these files hold pairing secrets."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def load_or_create_pairing():
    pairing = cli.read_json(PAIRING_FILE, {})
    if not pairing.get("setup_code") or not pairing.get("setup_id"):
        while True:
            digits = "".join(str(secrets.randbelow(10)) for _ in range(8))
            if digits not in WEAK_CODES:
                break
        pairing = {
            "setup_code": "%s-%s-%s" % (digits[:3], digits[3:5], digits[5:]),
            "setup_id": "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4)),
        }
        private_write(PAIRING_FILE, pairing)
    return pairing


# ---------------------------------------------------------------- the light

class LightLink:
    """Holds the wanted state and sends it to the light from one worker thread.

    Home sends a burst of writes while a slider moves. The worker always sends
    the newest wanted state, so a burst collapses instead of queueing up.
    """

    KEYS = ("on", "mode", "kelvin", "brightness", "tint", "curve", "hue", "saturation")

    def __init__(self):
        settings = cli.read_json(cli.LIGHT_FILE, {})
        self.profile = cli.light_profile(settings)
        self.bridge = cli.Bridge(settings.get("bridge") or cli.DEFAULT_BRIDGE)
        self.mac = cli.light_address(settings, None)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.reachable = False
        self.link_age = None
        self.wanted = self.normalise(cli.read_json(cli.STATE_FILE, {}), self.profile)
        self.sent_on = self.wanted["on"]
        self.sent_frame = None
        self.state_stamp = self.stamp()
        self.sunrise = None  # a Sunrise, stopped whenever Home sets the light
        self.fresh = False   # the setting being sent is a fresh start, to be recorded as the follower's
        self.tracking_since = cli.tracking()["since"]
        self.thread = threading.Thread(target=self.run, name="light-sender", daemon=True)

    @property
    def serial(self):
        return "".join("%02X" % byte for byte in self.mac[3:])

    @classmethod
    def normalise(cls, state, profile):
        """A state as this light can show it: inside its range, with its curve, and white
        if it has no colour LEDs."""
        mode = state.get("mode")
        curve = state.get("curve") if state.get("curve") in cli.CURVES else "linear"
        normal = {
            "on": bool(state.get("on")),
            "mode": mode if mode in ("white", "colour") else "white",
            "kelvin": cli.fit_kelvin(profile, state.get("kelvin", 5600)),
            # The setting as last sent: in tenths on a light that takes them.
            "brightness": (cli.as_setting(profile, state.get("brightness", 10)) if mode == "white" else
                           int(state.get("brightness", 10))),
            "tint": int(state.get("tint", 0)),
            "curve": curve if profile["firmware_curves"] else profile["curve"] or curve,
            "hue": int(state.get("hue", 0)),
            "saturation": int(state.get("saturation", 100)),
            # Light output as a percent of full, and what the light was last sent.
            "output": cli.output_of(profile, state) if mode == "white" else float(state.get("brightness", 10)),
            "shown": cli.shown_of(state),
        }
        if normal["mode"] == "colour" and not profile["colour"]:
            normal.update(mode="white", kelvin=cli.nearest_white(profile, normal["hue"], normal["saturation"]))
        return normal

    def home_brightness(self, state):
        """Home's brightness for a state, a whole number, at least 1 while lit: on a light with a dial
        (the AS600B) its setting on its own curve, as on the dial; on others light output."""
        if state["mode"] == "colour" and self.profile["colour"]:
            return state["brightness"]
        output = state["output"]
        if output <= 0:
            return 0
        level = cli.dial_setting(self.profile, output) if self.profile["dial"] else output
        return max(1, int(round(level)))

    @staticmethod
    def stamp():
        try:
            return os.stat(cli.STATE_FILE).st_mtime_ns
        except OSError:
            return None

    def snapshot(self):
        with self.lock:
            return dict(self.wanted)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping = True
        self.wake.set()

    # -- from Apple Home

    def apply_home(self, values):
        with self.lock:
            wanted = self.wanted
            # A fresh start: switched on by day, the light goes straight to
            # the sun's setting, not back to how it was left.
            fresh = cli.fresh_start_look() if values.get("On") and not wanted["on"] else None
            if fresh is not None:
                wanted.update(on=True, mode="white", kelvin=fresh[0], output=float(fresh[1]), tint=0)
                self.fresh = True
                values = {}
            before = (cli.follow_look(wanted), wanted["output"])
            # Home may send a temperature together with a matching hue and
            # saturation; the temperature is the one the user chose.
            if "ColorTemperature" in values:
                wanted["mode"] = "white"
                wanted["kelvin"] = mired_to_kelvin(values["ColorTemperature"], self.profile)
            elif "Hue" in values or "Saturation" in values:
                if "Hue" in values:
                    wanted["hue"] = int(round(values["Hue"])) % 360
                if "Saturation" in values:
                    wanted["saturation"] = max(0, min(100, int(round(values["Saturation"]))))
                if self.profile["colour"]:
                    wanted["mode"] = "colour"
                else:
                    # White LEDs only: the colour asked for becomes the nearest
                    # white, and Home is shown that white.
                    wanted["mode"] = "white"
                    wanted["kelvin"] = cli.nearest_white(self.profile, wanted["hue"], wanted["saturation"])
            if "Brightness" in values:
                # The dial setting on the AS600B (Home 20 is exp 20.0, 1.3% output),
                # light output on others. Home also writes back the number it was
                # shown; that is not a change, and must not round a fraction away.
                level = max(0, min(100, int(values["Brightness"])))
                if level != self.home_brightness(wanted):
                    dial = self.profile["dial"] and wanted["mode"] == "white"
                    wanted["output"] = cli.dial_output(self.profile, level) if dial else float(level)
                    if wanted["mode"] == "colour":
                        wanted["brightness"] = level
            if "On" in values:
                wanted["on"] = bool(values["On"])
            if (cli.follow_look(wanted), wanted["output"]) != before and cli.tracking_settings()["switch"]:
                # A colour or brightness from Home: sun tracking goes off, for
                # everything, until it is switched on again.
                log.info("sun tracking off: %s set from Apple Home", cli.describe(wanted))
                cli.set_tracking(False, "light", "set from Apple Home: %s" % cli.describe(wanted))
        self.wake.set()

    # -- to the light

    def frames(self, state):
        """The frames to show a state, from what the light was last sent: [(frame, seconds to wait after)],
        and the look to record. A change to the exponential curve can take two frames (see cli.plan_white)."""
        if state["mode"] == "colour" and self.profile["colour"]:
            return [(cli.colour_frame(self.mac, state["hue"], state["saturation"], state["brightness"]), 0.0)], {}
        kelvin = state["kelvin"] if state["mode"] == "white" else cli.nearest_white(
            self.profile, state["hue"], state["saturation"])
        steps, look = cli.plan_white(self.profile, state, kelvin, state["output"], state["tint"])
        return [(cli.white_frame(self.mac, k, number, tint, curve, self.profile["tenths"]), wait)
                for k, number, tint, curve, wait in steps], look

    def mode_frame(self, state):
        """The frame the light last got, for a state as recorded."""
        if state["mode"] == "colour" and self.profile["colour"]:
            return cli.colour_frame(self.mac, state["hue"], state["saturation"], state["brightness"])
        shown = state["shown"]
        if None in (shown["curve"], shown["brightness"], shown["kelvin"]):
            return None
        return cli.white_frame(self.mac, shown["kelvin"], cli.as_setting(self.profile, shown["brightness"]),
                               state["tint"], shown["curve"], self.profile["tenths"])

    def run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            if self.stopping:
                return
            if self.sunrise is not None:
                # A change from Home wins over a running sunrise. The ramp would
                # notice by itself at its next step, but that can be a minute away.
                self.sunrise.stop(wait=3)
            target = self.snapshot()
            try:
                self.send(target)
            except cli.Failure as failure:
                log.warning("could not send to the light: %s", failure)
                # Forget what was sent so the next change is sent in full.
                self.sent_on = None
                self.sent_frame = None

    def send(self, target):
        if not target["on"]:
            if self.sent_on is not False:
                self.bridge.send(cli.power_frame(self.mac, False))
                self.sent_on = False
        else:
            woke = False
            if self.sent_on is not True:
                self.bridge.send(cli.power_frame(self.mac, True))
                self.sent_on = True
                woke = True
                time.sleep(0.3)  # a light coming out of standby ignores what follows too closely
            frames, look = self.frames(target)
            if woke or frames[-1][0] != self.sent_frame:
                for frame, wait in frames:
                    self.bridge.send(frame)
                    if wait:
                        time.sleep(wait)
                self.sent_frame = frames[-1][0]
            if look:
                target.update(brightness=look["brightness"], curve=look["curve"], output=look["output"],
                              shown=look["shown"])
                with self.lock:
                    self.wanted["shown"] = look["shown"]
                    if (self.wanted["kelvin"], self.wanted["output"]) == (target["kelvin"], target["output"]):
                        self.wanted.update(brightness=look["brightness"], curve=look["curve"])
        cli.write_json(cli.STATE_FILE, target)
        self.state_stamp = self.stamp()
        if self.fresh:
            self.fresh = False
            cli.fresh_start_done()
            log.info("fresh start: switched on at %s", cli.describe(target))

    # -- housekeeping, called on a timer

    def refresh_link(self):
        """Asks the bridge whether the light is connected."""
        try:
            status, info = self.bridge.json("GET", "/status")
            reachable = bool(info.get("connected"))
            link_age = info.get("connectedSeconds")
        except cli.Failure:
            reachable, link_age = False, None
        if reachable and (self.link_age is None or link_age is None or link_age < self.link_age):
            # A younger link means it was rebuilt; the light may have been
            # power-cycled, so do not assume it still holds the last frame.
            self.sent_frame = None
        if reachable != self.reachable:
            log.info("light is %s", "reachable" if reachable else "not reachable")
        self.link_age = link_age if reachable else None
        self.reachable = reachable

    def watch_tracking(self):
        """When sun tracking is switched on, sets the light from the sun at once, not at the next minute."""
        track = cli.tracking()
        if track["since"] == self.tracking_since:
            return
        self.tracking_since = track["since"]
        if track["on"] and cli.tracking_settings()["switch"]:
            subprocess.Popen([sys.executable, "-u", CLI_FILE, "follow", "apply"], stdin=subprocess.DEVNULL)

    def adopt_outside_change(self):
        """Picks up a state change made by the `neewer` command. True if there was one."""
        stamp = self.stamp()
        if stamp == self.state_stamp:
            return False
        self.state_stamp = stamp
        raw = cli.read_json(cli.STATE_FILE, {})
        on_disk = self.normalise(raw, self.profile)
        # Home has no way to show the command's "channels" mode (used for levels
        # below 1%), so it is shown as white. Forgetting the last frame makes
        # sure the next change from Home is really sent.
        shown_exactly = raw.get("mode") in ("white", "colour")
        with self.lock:
            changed = on_disk != self.wanted
            self.wanted = on_disk
            self.sent_on = on_disk["on"]
            self.sent_frame = self.mode_frame(on_disk) if on_disk["on"] and shown_exactly else None
        return changed


# ---------------------------------------------------------------- sunrise

def sunrise_command(settings):
    """The `neewer sunrise` command line, with options from homekit.json.

    "sunrise": {"minutes": 30, "brightness": 100, "kelvin": 5600, "style": "sky"} in homekit.json
    sets the ramp; any of them may be left out. The options are checked with
    the command's own parser now, so a mistake shows in the log at start, not at dawn.
    """
    given = settings.get("sunrise")
    given = given if isinstance(given, dict) else {}
    arguments = ["sunrise"]
    for key in ("minutes", "brightness", "kelvin", "style"):
        if key in given:
            arguments += ["--" + key, str(given[key])]
    try:
        cli.build_parser().parse_args(arguments)  # prints what is wrong, then exits
    except SystemExit:
        log.error("homekit.json: ignoring the sunrise settings %s and using the defaults", given)
        arguments = ["sunrise"]
    return [sys.executable, "-u", CLI_FILE] + arguments


class Sunrise:
    """Runs one `neewer sunrise` at a time for the Sunrise switch.

    The ramp is the command itself, run as a child process, so it behaves exactly
    as it does in a terminal: it stops when the light is set any other way, and
    waits out a lost link. Its output goes to this service's log.
    """

    def __init__(self, command):
        self.command = command
        self.process = None
        self.lock = threading.Lock()

    def running(self):
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            log.info("sunrise: starting (%s)", " ".join(self.command[3:]))
            self.process = subprocess.Popen(
                self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self.relay, args=(self.process,), name="sunrise-log", daemon=True).start()

    @staticmethod
    def relay(process):
        for line in process.stdout:
            log.info("sunrise: %s", line.rstrip())
        code = process.wait()
        if code:
            log.warning("sunrise: ended with exit status %d", code)

    def stop(self, wait=0):
        with self.lock:
            process = self.process
        if process is None or process.poll() is not None:
            return
        # Ctrl-C, in effect: the command stops where it is and says so.
        process.send_signal(signal.SIGINT)
        if wait:
            try:
                process.wait(wait)
            except subprocess.TimeoutExpired:
                process.kill()


# ---------------------------------------------------------------- the accessory

def build_accessory(driver, name, light, sunrise):
    from pyhap.accessory import Accessory
    from pyhap.const import CATEGORY_LIGHTBULB

    class NeewerLight(Accessory):
        category = CATEGORY_LIGHTBULB

        def __init__(self):
            super().__init__(driver, name)
            self.set_info_service(
                firmware_revision=VERSION, manufacturer="NEEWER", model=light.profile["model"],
                serial_number=light.serial)
            service = self.add_preload_service(
                "Lightbulb", chars=["Brightness", "Hue", "Saturation", "ColorTemperature"])
            self.on = service.configure_char("On")
            self.brightness = service.configure_char("Brightness")
            self.hue = service.configure_char("Hue")
            self.saturation = service.configure_char("Saturation")
            self.temperature = service.configure_char(
                "ColorTemperature", properties=dict(zip(("minValue", "maxValue"), mired_range(light.profile))))
            service.setter_callback = self.home_wrote
            service.is_primary_service = True
            # Added after the light's own service, so the light keeps its
            # characteristic ids and stays paired.
            switch = self.add_preload_service("Switch", chars=["Name"])
            switch.configure_char("Name", value="Sunrise")
            self.sunrise_on = switch.configure_char("On", value=False, setter_callback=self.sunrise_wrote)
            self.address_at_start = driver.state.address
            self.was_paired = driver.state.paired
            self.ticks = 0
            self.show(light.snapshot(), notify=False)

        @property
        def available(self):
            # False makes Home show "No Response" instead of a state it cannot change.
            return light.reachable

        def setup_message(self):
            # The default prints the setup code and a QR code into the log.
            log.info("not paired yet; run this file with --pairing to see the setup code")

        def show(self, state, notify, skip=()):
            """Sets Home's view of the light from a state."""
            if state["mode"] == "colour":
                hue, saturation = state["hue"], state["saturation"]
            else:
                hue, saturation = kelvin_to_hue_saturation(state["kelvin"])
            values = (
                ("On", self.on, state["on"]),
                ("Brightness", self.brightness, light.home_brightness(state)),
                ("Hue", self.hue, hue),
                ("Saturation", self.saturation, saturation),
                ("ColorTemperature", self.temperature, kelvin_to_mired(state["kelvin"], light.profile)),
            )
            for label, characteristic, value in values:
                if label not in skip and characteristic.get_value() != value:
                    characteristic.set_value(value, should_notify=notify)

        def home_wrote(self, values):
            if not light.reachable:
                # Raising reports a failure to Home, which is the truth.
                raise RuntimeError("the light is not connected")
            light.apply_home(values)
            # Fill in the values Home did not send, such as the hue of a new white.
            self.show(light.snapshot(), notify=True, skip=values.keys())

        def sunrise_wrote(self, value):
            # Started even if the light is out of reach right now: the ramp
            # waits for it, which suits an automation that fires at dawn.
            if value:
                sunrise.start()
            else:
                sunrise.stop()

        @Accessory.run_at_interval(3)
        def run(self):
            light.refresh_link()
            light.watch_tracking()
            if light.adopt_outside_change():
                self.show(light.snapshot(), notify=True)
            running = sunrise.running()
            if self.sunrise_on.get_value() != running:
                self.sunrise_on.set_value(running, should_notify=True)
            paired = driver.state.paired
            if paired != self.was_paired:
                self.was_paired = paired
                log.info("now %s", "paired with Apple Home" if paired else "unpaired")
                record_pairing(driver, self)
            self.ticks += 1
            if self.ticks % 20 == 0:
                self.restart_if_address_changed()

        def restart_if_address_changed(self):
            from pyhap import util
            try:
                current = util.get_local_address()
            except OSError:
                return
            if current != self.address_at_start:
                # Home finds the accessory by the address announced at start.
                log.info("network address changed to %s; restarting to announce it", current)
                os.kill(os.getpid(), signal.SIGTERM)

        def stop(self):
            light.stop()
            sunrise.stop(wait=3)

    return NeewerLight()


def log_home_writes(driver):
    """Logs every value Apple Home writes, with the address it came from: one line a write.

    So a change nobody remembers making (sun tracking turned off by itself,
    say) can be traced to the iPhone, the home hub or an automation.
    """
    original = driver.set_characteristics

    def logged(chars_query, client_addr):
        parts = []
        for query in chars_query.get("characteristics", []):
            if "value" not in query:
                continue  # only asking to be told of changes
            char = driver.accessory.get_characteristic(query.get("aid"), query.get("iid"))
            if char is None:
                parts.append("aid %s iid %s=%r" % (query.get("aid"), query.get("iid"), query["value"]))
                continue
            owner = char.service.broker.display_name if char.service and char.service.broker else "?"
            service = char.service.display_name if char.service else "?"
            parts.append("%s %s %s=%r" % (owner, service, char.display_name, query["value"]))
        if parts:
            log.info("Home wrote (from %s): %s", client_addr[0] if client_addr else "?", ", ".join(parts))
        return original(chars_query, client_addr)
    driver.set_characteristics = logged


def record_pairing(driver, accessory):
    pairing = cli.read_json(PAIRING_FILE, {})
    pairing.update({
        "uri": accessory.xhm_uri(),
        "paired": bool(driver.state.paired),
        "name": accessory.display_name,
        "address": driver.state.address,
        "port": driver.state.port,
    })
    private_write(PAIRING_FILE, pairing)


# ---------------------------------------------------------------- entry points

def serve():
    from pyhap import util
    from pyhap.accessory_driver import AccessoryDriver

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stdout)
    os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)

    settings = cli.read_json(SETTINGS_FILE, {})
    name = settings.get("name") or DEFAULT_NAME
    port = int(settings.get("port") or DEFAULT_PORT)
    pairing = load_or_create_pairing()
    try:
        light = LightLink()
    except cli.Failure as failure:
        log.error("%s", failure)
        return 1

    address = util.get_local_address()
    driver = AccessoryDriver(
        address=address, port=port, persist_file=KEYS_FILE,
        pincode=pairing["setup_code"].encode("ascii"),
        # Announce on the home network only, not on VPN or tunnel interfaces.
        interface_choice=[address],
    )
    driver.state.setup_id = pairing["setup_id"]  # keeps the QR code valid across restarts
    log_home_writes(driver)
    sunrise = Sunrise(sunrise_command(settings))
    light.sunrise = sunrise
    accessory = build_accessory(driver, name, light, sunrise)
    driver.add_accessory(accessory)
    record_pairing(driver, accessory)

    signal.signal(signal.SIGTERM, driver.signal_handler)
    light.refresh_link()
    light.start()
    log.info("Neewer HomeKit %s: '%s' on %s:%d, %s", VERSION, name, address, port,
             "paired" if driver.state.paired else "waiting to be paired")
    driver.start()
    return 0


def show_pairing():
    pairing = cli.read_json(PAIRING_FILE, {})
    if not pairing.get("setup_code"):
        print("neewer-homekit: the service has not run yet, so there is no setup code", file=sys.stderr)
        return 1
    print(json.dumps(pairing, indent=2, sort_keys=True))
    return 0


def write_qr(path):
    pairing = cli.read_json(PAIRING_FILE, {})
    if not pairing.get("uri"):
        print("neewer-homekit: the service has not run yet, so there is no pairing code", file=sys.stderr)
        return 1
    import pyqrcode
    pyqrcode.create(pairing["uri"], error="M").svg(path, scale=10, quiet_zone=4, background="white")
    print(path)
    return 0


def main(argv):
    if len(argv) == 2 and argv[1] == "--pairing":
        return show_pairing()
    if len(argv) == 3 and argv[1] == "--qr":
        return write_qr(argv[2])
    if len(argv) == 1:
        return serve()
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
