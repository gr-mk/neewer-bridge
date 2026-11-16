#!/usr/bin/env python3
"""Presents the Neewer light to Apple Home as one colour light.

It runs as a child of the Neewer HomeKit launcher app and sends commands through
the local Neewer Bridge, using the same frame builder as the `neewer` command
(install.sh places a copy of that script next to this file as neewer_cli.py).

The light never reports its state, so Apple Home shows what was last sent, by
Home or by the `neewer` command. Changes made on the light's own dial are not seen.

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
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import neewer_cli as cli  # noqa: E402

VERSION = "1.0.0"
DATA_DIR = os.path.join(cli.APP_DIR, "homekit-data")
KEYS_FILE = os.path.join(DATA_DIR, "accessory.state")
PAIRING_FILE = os.path.join(DATA_DIR, "pairing.json")
SETTINGS_FILE = os.path.join(cli.APP_DIR, "homekit.json")

DEFAULT_NAME = "Neewer CB200C"
DEFAULT_PORT = 51826
MIRED_MIN = int(round(1e6 / cli.KELVIN_MAX))  # 133, the coolest white
MIRED_MAX = int(round(1e6 / cli.KELVIN_MIN))  # 400, the warmest white
# The HomeKit specification rejects these setup codes as too guessable.
WEAK_CODES = {str(d) * 8 for d in range(10)} | {"12345678", "87654321"}

log = logging.getLogger("neewer-homekit")


# ---------------------------------------------------------------- conversions

def mired_to_kelvin(mired):
    kelvin = int(round(1e6 / mired / 100.0)) * 100
    return max(cli.KELVIN_MIN, min(cli.KELVIN_MAX, kelvin))


def kelvin_to_mired(kelvin):
    return max(MIRED_MIN, min(MIRED_MAX, int(round(1e6 / kelvin))))


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
        self.bridge = cli.Bridge(settings.get("bridge") or cli.DEFAULT_BRIDGE)
        self.mac = cli.light_address(settings, None)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = False
        self.reachable = False
        self.link_age = None
        self.wanted = self.normalise(cli.read_json(cli.STATE_FILE, {}))
        self.sent_on = self.wanted["on"]
        self.sent_frame = None
        self.state_stamp = self.stamp()
        self.thread = threading.Thread(target=self.run, name="light-sender", daemon=True)

    @property
    def serial(self):
        return "".join("%02X" % byte for byte in self.mac[3:])

    @classmethod
    def normalise(cls, state):
        mode = state.get("mode")
        return {
            "on": bool(state.get("on")),
            "mode": mode if mode in ("white", "colour") else "white",
            "kelvin": int(state.get("kelvin", 5600)),
            "brightness": int(state.get("brightness", 10)),
            "tint": int(state.get("tint", 0)),
            "curve": state.get("curve") if state.get("curve") in cli.CURVES else "linear",
            "hue": int(state.get("hue", 0)),
            "saturation": int(state.get("saturation", 100)),
        }

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
            # Home may send a temperature together with a matching hue and
            # saturation; the temperature is the one the user chose.
            if "ColorTemperature" in values:
                wanted["mode"] = "white"
                wanted["kelvin"] = mired_to_kelvin(values["ColorTemperature"])
            elif "Hue" in values or "Saturation" in values:
                wanted["mode"] = "colour"
                if "Hue" in values:
                    wanted["hue"] = int(round(values["Hue"])) % 360
                if "Saturation" in values:
                    wanted["saturation"] = max(0, min(100, int(round(values["Saturation"]))))
            if "Brightness" in values:
                wanted["brightness"] = max(0, min(100, int(values["Brightness"])))
            if "On" in values:
                wanted["on"] = bool(values["On"])
        self.wake.set()

    # -- to the light

    def mode_frame(self, state):
        if state["mode"] == "colour":
            return cli.colour_frame(self.mac, state["hue"], state["saturation"], state["brightness"])
        return cli.white_frame(self.mac, state["kelvin"], state["brightness"], state["tint"], state["curve"])

    def run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            if self.stopping:
                return
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
            frame = self.mode_frame(target)
            if woke or frame != self.sent_frame:
                self.bridge.send(frame)
                self.sent_frame = frame
        cli.write_json(cli.STATE_FILE, target)
        self.state_stamp = self.stamp()

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

    def adopt_outside_change(self):
        """Picks up a state change made by the `neewer` command. True if there was one."""
        stamp = self.stamp()
        if stamp == self.state_stamp:
            return False
        self.state_stamp = stamp
        on_disk = self.normalise(cli.read_json(cli.STATE_FILE, {}))
        with self.lock:
            if on_disk == self.wanted:
                return False
            self.wanted = on_disk
            self.sent_on = on_disk["on"]
            self.sent_frame = self.mode_frame(on_disk) if on_disk["on"] else None
        return True


# ---------------------------------------------------------------- the accessory

def build_accessory(driver, name, light):
    from pyhap.accessory import Accessory
    from pyhap.const import CATEGORY_LIGHTBULB

    class NeewerLight(Accessory):
        category = CATEGORY_LIGHTBULB

        def __init__(self):
            super().__init__(driver, name)
            self.set_info_service(
                firmware_revision=VERSION, manufacturer="NEEWER", model="CB200C", serial_number=light.serial)
            service = self.add_preload_service(
                "Lightbulb", chars=["Brightness", "Hue", "Saturation", "ColorTemperature"])
            self.on = service.configure_char("On")
            self.brightness = service.configure_char("Brightness")
            self.hue = service.configure_char("Hue")
            self.saturation = service.configure_char("Saturation")
            self.temperature = service.configure_char(
                "ColorTemperature", properties={"minValue": MIRED_MIN, "maxValue": MIRED_MAX})
            service.setter_callback = self.home_wrote
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
                ("Brightness", self.brightness, state["brightness"]),
                ("Hue", self.hue, hue),
                ("Saturation", self.saturation, saturation),
                ("ColorTemperature", self.temperature, kelvin_to_mired(state["kelvin"])),
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

        @Accessory.run_at_interval(3)
        def run(self):
            light.refresh_link()
            if light.adopt_outside_change():
                self.show(light.snapshot(), notify=True)
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

    return NeewerLight()


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
    accessory = build_accessory(driver, name, light)
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
