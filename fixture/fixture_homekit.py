#!/usr/bin/env python3
"""Presents the fixture to Apple Home as one colour light.

It runs as a child of the Fixture HomeKit launcher app and drives the bulbs
with the same code as the `fixture` command (install.sh places a copy of that
script next to this file as fixture_cli.py).

The bulbs are asked what they are doing every few seconds, so a change made
with the command, the sun follower, the WiZ app or a bulb's own tile shows up
in Home.

    fixture_homekit.py            run the service
    fixture_homekit.py --pairing  print the setup code and pairing state as JSON
    fixture_homekit.py --qr FILE  write the pairing QR code to FILE as SVG
"""

import colorsys
import json
import logging
import math
import os
import secrets
import signal
import string
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fixture_cli as cli  # noqa: E402

VERSION = "1.0.0"
POLL_SECONDS = 5
SEARCH_SECONDS = 30
# A burst of writes from Home (a slider being dragged) is over when nothing
# new has come for this long.
BURST_QUIET_SECONDS = 0.35
pause = time.sleep      # the sender waits with this, and
clock = time.monotonic  # measures with this; the tests replace both
# The HomeKit specification rejects these setup codes as too guessable.
WEAK_CODES = {str(d) * 8 for d in range(10)} | {"12345678", "87654321"}

log = logging.getLogger("fixture-homekit")


def data_dir():
    return cli.path("homekit-data")


def keys_file():
    return os.path.join(data_dir(), "accessory.state")


def pairing_file():
    return os.path.join(data_dir(), "pairing.json")


# ---------------------------------------------------------------- conversions

def mired_range(settings):
    """(coolest, warmest) in mireds, both inside what the bulbs can show."""
    return int(math.ceil(1e6 / settings["kelvin_max"])), int(math.floor(1e6 / settings["kelvin_min"]))


def kelvin_to_mired(kelvin, settings):
    low, high = mired_range(settings)
    return cli.clamp(low, int(round(1e6 / kelvin)), high)


def kelvin_to_hue_saturation(kelvin):
    """Approximate colour of a white point, so Home's colour wheel matches the light."""
    t = kelvin / 100.0
    if t <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
        blue = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        red = 329.698727446 * (t - 60) ** -0.1332047592
        green = 288.1221695283 * (t - 60) ** -0.0755148492
        blue = 255.0
    red, green, blue = (max(0.0, min(255.0, c)) / 255.0 for c in (red, green, blue))
    hue, saturation, _ = colorsys.rgb_to_hsv(red, green, blue)
    return int(round(hue * 360)) % 360, int(round(saturation * 100))


def home_values(setting, settings):
    """Home's five characteristics for a fixture setting."""
    if setting["mode"] == "colour":
        hue, saturation = setting["hue"], setting["saturation"]
    else:
        hue, saturation = kelvin_to_hue_saturation(setting["kelvin"])
    return {
        "On": bool(setting["on"]),
        # Home's brightness is a whole number. A fraction is shown rounded,
        # and anything lit shows as at least 1.
        "Brightness": cli.clamp(1, int(round(setting["brightness"])), 100),
        "Hue": hue,
        "Saturation": saturation,
        "ColorTemperature": kelvin_to_mired(setting["kelvin"], settings),
    }


def apply_home_values(setting, values, fixture):
    """A fixture setting changed by what Home wrote. Returns the new setting."""
    setting = dict(setting)
    # Home may send a temperature together with a matching hue and
    # saturation; the temperature is the one the user chose.
    if "ColorTemperature" in values:
        setting["mode"] = "white"
        setting["kelvin"] = fixture.kelvin(1e6 / values["ColorTemperature"])
    elif "Hue" in values or "Saturation" in values:
        setting["mode"] = "colour"
        if "Hue" in values:
            setting["hue"] = int(round(values["Hue"])) % 360
        if "Saturation" in values:
            setting["saturation"] = cli.clamp(0, int(round(values["Saturation"])), 100)
    if "Brightness" in values:
        # Home sends 0 only together with "off"; the bulbs have no 0.
        # A whole number from Home is every bulb at that dimming. Home also
        # writes back the number it was shown; that is not a change, and must
        # not round a fraction away.
        if int(values["Brightness"]) != cli.clamp(1, int(round(setting["brightness"])), 100):
            setting["brightness"] = float(cli.clamp(1, int(values["Brightness"]), 100))
    if "On" in values:
        setting["on"] = bool(values["On"])
    return setting


# ---------------------------------------------------------------- pairing record

def private_write(file, value):
    """Writes JSON readable only by this user: these files hold pairing secrets."""
    os.makedirs(os.path.dirname(file), mode=0o700, exist_ok=True)
    temporary = file + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, file)


def load_or_create_pairing():
    pairing = cli.read_json(pairing_file(), {})
    if not pairing.get("setup_code") or not pairing.get("setup_id"):
        while True:
            digits = "".join(str(secrets.randbelow(10)) for _ in range(8))
            if digits not in WEAK_CODES:
                break
        pairing = {
            "setup_code": "%s-%s-%s" % (digits[:3], digits[3:5], digits[5:]),
            "setup_id": "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4)),
        }
        private_write(pairing_file(), pairing)
    return pairing


# ---------------------------------------------------------------- the light

class Link:
    """Holds the wanted setting and sends it to the bulbs from one worker thread.

    Home sends a burst of writes while a slider moves. During the burst the
    worker streams: it sends the newest wanted setting to every bulb at a
    steady rate and does not wait for the bulbs to answer, so the light keeps
    up with the finger. When the burst goes quiet it sends the last setting
    once more the careful way, waiting for every bulb and trying again for
    any that missed it, and only then writes the record.
    """

    def __init__(self, fixture):
        self.fixture = fixture
        fixture.search_every = SEARCH_SECONDS
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.reachable = False
        self.unsent = False  # Home wrote something the worker has not sent yet
        self.trouble = None
        self.refused = False  # macOS has not allowed the local network yet
        self.owner = "home"   # who the setting being sent belongs to: "follow" for a fresh start
        self.tracking_since = cli.tracking()["since"]
        self.wanted = fixture.state()["setting"]
        self.thread = threading.Thread(target=self.run, name="fixture-sender", daemon=True)

    @property
    def serial(self):
        return "FX-" + self.fixture.macs[0][-6:].upper()

    def snapshot(self):
        with self.lock:
            return dict(self.wanted)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping = True
        self.wake.set()

    def apply_home(self, values):
        with self.lock:
            # A fresh start: switched on by day, the fixture goes straight to
            # the sun's setting, not back to how it was left, and is the
            # follower's from then on.
            fresh = cli.fresh_start_setting(self.fixture) if values.get("On") and not self.wanted["on"] else None
            if fresh is not None:
                self.wanted, self.owner = fresh, "follow"
            else:
                self.wanted, self.owner = apply_home_values(self.wanted, values, self.fixture), "home"
            self.unsent = True
        self.wake.set()

    def run(self):
        while True:
            self.wake.wait()
            if self.stopping:
                return
            self.send_burst()

    def send_burst(self):
        """Streams what Home writes until it goes quiet, then sends the last setting for certain."""
        rate = self.fixture.settings["stream_rate"]
        self.wake.clear()
        if rate <= 0:
            return self.send_wanted()  # no streaming: every send waits for the bulbs
        # The lock is held for the whole burst, so the command and the sun
        # follower wait their turn and nothing else sends in between.
        with self.fixture.locked():
            sock = cli.open_socket()
            try:
                last, changed = None, clock()
                while not self.stopping:
                    self.wake.clear()
                    target = self.snapshot()
                    if target != last:
                        self.fixture.stream(target, sock)
                        last, changed = target, clock()
                    elif clock() - changed >= BURST_QUIET_SECONDS:
                        break
                    pause(1.0 / rate)
            finally:
                sock.close()
            self.send_wanted()

    def send_wanted(self):
        with self.lock:
            target, owner = dict(self.wanted), self.owner
        try:
            missed = self.fixture.apply(target, owner)
            if owner == "follow":
                self.fixture.claim("follow")
                self.fixture.fresh_done()
                log.info("fresh start: switched on at %s", self.fixture.describe(target))
            # A colour or brightness from Home turns sun tracking off.
            cli.custom_check(self.fixture)
        except cli.Failure as failure:
            self.note_trouble(str(failure))
            return
        finally:
            with self.lock:
                # Anything Home wrote during the send has set the event again.
                self.unsent = self.wanted != target
        self.note_trouble("not answering: %s" % ", ".join(missed) if missed else None)

    def follow_now(self):
        """One run of the sun follower, now and not at the next minute."""
        line = cli.follow_apply(self.fixture)
        if line:
            log.info("%s", line)

    def watch_tracking(self):
        """Sets the fixture from the sun at once when sun tracking is switched on, wherever that was done."""
        track = cli.tracking()
        if track["since"] == self.tracking_since:
            return
        self.tracking_since = track["since"]
        if track["on"] and cli.tracking_settings()["switch"]:
            self.follow_now()

    def note_trouble(self, trouble):
        # Said once when it starts and once when it ends, not on every try.
        if trouble != self.trouble:
            log.warning("%s", trouble) if trouble else log.info("every bulb is answering again")
            self.trouble = trouble

    def poll(self):
        """Asks the bulbs what they are doing. True if the setting changed elsewhere."""
        if self.unsent:
            return False  # Home is setting the fixture right now; do not add to the traffic
        try:
            state, pilots = self.fixture.reconcile()
        except cli.Failure as failure:
            self.set_reachable(False)
            self.note_trouble(str(failure))
            self.refused = self.refused or str(failure) == cli.LOCAL_NETWORK_HELP
            return False
        if self.refused and not self.stopping:
            # The announcement to Apple Home was refused as well, at start.
            # launchd starts the service again, and this time it goes out.
            log.info("the local network is allowed now; restarting to announce to Apple Home")
            self.stopping = True
            os.kill(os.getpid(), signal.SIGTERM)
        self.set_reachable(True)
        missing = [mac for mac in self.fixture.macs if pilots[mac] is None]
        self.note_trouble("not answering: %s" % ", ".join(missing) if missing else None)
        cli.custom_check(self.fixture)  # set in the WiZ app or on a bulb's own tile: tracking goes off
        with self.lock:
            if self.unsent:
                return False  # Home's newer wish is about to be sent
            changed = state["setting"] != self.wanted
            self.wanted = state["setting"]
        return changed

    def set_reachable(self, reachable):
        if reachable != self.reachable:
            log.info("the fixture is %s", "reachable" if reachable else "not reachable")
        self.reachable = reachable


# ---------------------------------------------------------------- the accessory

def build_accessory(driver, name, link):
    from pyhap.accessory import Accessory
    from pyhap.const import CATEGORY_LIGHTBULB

    settings = link.fixture.settings
    coolest, warmest = mired_range(settings)

    class FixtureLight(Accessory):
        category = CATEGORY_LIGHTBULB

        def __init__(self):
            super().__init__(driver, name)
            self.set_info_service(
                firmware_revision=VERSION, manufacturer="WiZ",
                model="Fixture of %d bulbs" % len(link.fixture.macs), serial_number=link.serial)
            service = self.add_preload_service(
                "Lightbulb", chars=["Brightness", "Hue", "Saturation", "ColorTemperature"])
            self.chars = {
                "On": service.configure_char("On"),
                "Brightness": service.configure_char("Brightness"),
                "Hue": service.configure_char("Hue"),
                "Saturation": service.configure_char("Saturation"),
                "ColorTemperature": service.configure_char(
                    "ColorTemperature", properties={"minValue": coolest, "maxValue": warmest}),
            }
            service.setter_callback = self.home_wrote
            service.is_primary_service = True
            # Added after the light's own service, so the light keeps its
            # characteristic ids and stays paired.
            switch = self.add_preload_service("Switch", chars=["Name"])
            switch.configure_char("Name", value="Sun Tracking")
            self.tracking = switch.configure_char(
                "On", value=cli.tracking()["on"], setter_callback=self.tracking_wrote)
            self.address_at_start = driver.state.address
            self.was_paired = driver.state.paired
            self.ticks = 0
            self.show(link.snapshot(), notify=False)

        @property
        def available(self):
            # False makes Home show "No Response" instead of a state it cannot change.
            return link.reachable

        def setup_message(self):
            # The default prints the setup code and a QR code into the log.
            log.info("not paired yet; run this file with --pairing to see the setup code")

        def show(self, setting, notify, skip=()):
            """Sets Home's view of the fixture from a setting."""
            for label, value in home_values(setting, settings).items():
                characteristic = self.chars[label]
                if label not in skip and characteristic.get_value() != value:
                    characteristic.set_value(value, should_notify=notify)

        def home_wrote(self, values):
            if not link.reachable:
                # Raising reports a failure to Home, which is the truth.
                raise RuntimeError("no bulb is answering")
            link.apply_home(values)
            # Fill in the values Home did not send, such as the hue of a new white.
            self.show(link.snapshot(), notify=True, skip=values.keys())

        def tracking_wrote(self, value):
            # The switch for everything that follows the sun. Home's "on" is
            # not taken just after a device was set by hand (a scene's colours
            # win); the tile is put right at the next look.
            cli.set_tracking(bool(value), "home")

        @Accessory.run_at_interval(POLL_SECONDS)
        def run(self):
            link.watch_tracking()
            if self.tracking.get_value() != cli.tracking()["on"]:
                self.tracking.set_value(cli.tracking()["on"], should_notify=True)
            if link.poll():
                self.show(link.snapshot(), notify=True)
            paired = driver.state.paired
            if paired != self.was_paired:
                self.was_paired = paired
                log.info("now %s", "paired with Apple Home" if paired else "unpaired")
                record_pairing(driver, self)
            self.ticks += 1
            if self.ticks % 12 == 0:
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
            link.stop()

    return FixtureLight()


def record_pairing(driver, accessory):
    pairing = cli.read_json(pairing_file(), {})
    pairing.update({
        "uri": accessory.xhm_uri(),
        "paired": bool(driver.state.paired),
        "name": accessory.display_name,
        "address": driver.state.address,
        "port": driver.state.port,
    })
    private_write(pairing_file(), pairing)


# ---------------------------------------------------------------- entry points

def serve():
    from pyhap import util
    from pyhap.accessory_driver import AccessoryDriver

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stdout)
    os.makedirs(data_dir(), mode=0o700, exist_ok=True)
    os.chmod(data_dir(), 0o700)

    pairing = load_or_create_pairing()
    try:
        fixture = cli.Fixture()
    except cli.Failure as failure:
        log.error("%s", failure)
        return 1
    name, port = fixture.settings["name"], fixture.settings["port"]
    link = Link(fixture)

    address = util.get_local_address()
    driver = AccessoryDriver(
        address=address, port=port, persist_file=keys_file(),
        pincode=pairing["setup_code"].encode("ascii"),
        # Announce on the home network only, not on VPN or tunnel interfaces.
        interface_choice=[address],
    )
    driver.state.setup_id = pairing["setup_id"]  # keeps the QR code valid across restarts
    link.poll()
    accessory = build_accessory(driver, name, link)
    driver.add_accessory(accessory)
    record_pairing(driver, accessory)

    signal.signal(signal.SIGTERM, driver.signal_handler)
    link.start()
    log.info("Fixture HomeKit %s: '%s' (%d bulbs) on %s:%d, %s", VERSION, name, len(fixture.macs), address, port,
             "paired" if driver.state.paired else "waiting to be paired")
    driver.start()
    return 0


def show_pairing():
    pairing = cli.read_json(pairing_file(), {})
    if not pairing.get("setup_code"):
        print("fixture-homekit: the service has not run yet, so there is no setup code", file=sys.stderr)
        return 1
    print(json.dumps(pairing, indent=2, sort_keys=True))
    return 0


def write_qr(file):
    pairing = cli.read_json(pairing_file(), {})
    if not pairing.get("uri"):
        print("fixture-homekit: the service has not run yet, so there is no pairing code", file=sys.stderr)
        return 1
    import pyqrcode
    pyqrcode.create(pairing["uri"], error="M").svg(file, scale=10, quiet_zone=4, background="white")
    os.chmod(file, 0o600)  # the code lets anyone on the network pair
    print(file)
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
