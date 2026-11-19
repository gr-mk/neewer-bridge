#!/usr/bin/env python3
"""Presents the desk to Apple Home: the monitors as one light, the keyboard as another.

Both are one Apple Home bridge, "Desk", so there is one thing to pair, and
each shows as its own tile.

Monitors: brightness and colour temperature of every external display. They
are sundial's to set (~/bin/sundial runs every minute and follows the sun), so
this never touches a display itself. A change from Home is handed to sundial
as `sundial manual`, which holds it for two hours and then follows the sun
again. Home is shown what sundial last applied.

Keyboard: the Wooting keyboard's lighting. That is wooting-workspaces' to set
(it is the one program that writes the keyboard's LEDs), so this only writes
a small control file that program reads: on or off, a brightness, a tint and
a base colour. Left alone, the keyboard follows the sun as the lights do: dim
and warm at night, full by day. A change from Home is held for two hours.

    desk_homekit.py            run the service
    desk_homekit.py --pairing  print the setup code and pairing state as JSON
    desk_homekit.py --qr FILE  write the pairing QR code to FILE as SVG
    desk_homekit.py --status   say what the monitors and the keyboard are doing
"""

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
from datetime import datetime

VERSION = "1.0.0"
APP_DIR = os.environ.get("DESK_DIR") or os.path.expanduser("~/Library/Application Support/Desk")
SUNDIAL = os.path.expanduser("~/bin/sundial")
SUNDIAL_STATE_FILE = os.path.expanduser("~/.local/state/sundial/state.json")
SUNDIAL_STALE_SECONDS = 300
M1DDC = "/opt/homebrew/bin/m1ddc"
CONTROL_FILE = os.path.expanduser("~/.local/state/wooting-workspaces/control")
POLL_SECONDS = 3
DISPLAY_CHECK_SECONDS = 30
# The HomeKit specification rejects these setup codes as too guessable.
WEAK_CODES = {str(d) * 8 for d in range(10)} | {"12345678", "87654321"}

# What Night Shift can do: 2700K is the warmest macOS allows, 6000K the
# coolest tint, and anything cooler means no tint at all, shown as 6500K.
KELVIN_MIN, KELVIN_TINT_MAX, KELVIN_MAX = 2700, 6000, 6500
MIRED_MIN, MIRED_MAX = int(math.ceil(1e6 / KELVIN_MAX)), int(math.floor(1e6 / KELVIN_MIN))  # 154, 370

SETTINGS_DEFAULTS = {
    "name": "Desk",   # the bridge's name in Apple Home
    "port": 51828,
    "monitors": {
        "name": "Monitors",
        "hold_minutes": 120,   # how long a change from Home is kept before the sun takes over again
        "off_means": "dim",    # "dim": off is brightness 0, the backlight at its lowest. "sleep": off sleeps the displays
    },
    "keyboard": {
        "name": "Keyboard",
        "hold_minutes": 120,
        "day_brightness": 100,     # percent, with the sun well up
        "night_brightness": 25,    # percent after dark: low, but the workspace keys still read
        "base_colour": "ffffff",   # what the keys wooting-workspaces does not colour show while dimmed or tinted
        # At full brightness with no tint those keys are left to the Wootility
        # profile. The keyboard draws the profile itself and it cannot be
        # dimmed from outside, so at any other time the base colour replaces it.
        "keep_profile_at_full": True,
    },
}

# Sun tracking: the one switch for everything that follows the sun (see the
# fixture's follower). While it is off, the monitors and the keyboard stay as
# they are; setting either by hand turns it off.
TRACKING_DIR = os.path.expanduser("~/Library/Application Support/SunTracking")
FOREVER_MINUTES = 5256000   # ten years: a hold with no end, as sundial counts
FOREVER = 4102444800        # the year 2100, for the keyboard's own hold

log = logging.getLogger("desk-homekit")


def path(name):
    return os.path.join(APP_DIR, name)


def read_json(file, fallback):
    try:
        with open(file) as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def write_json(file, value, private=False):
    os.makedirs(os.path.dirname(file), mode=0o700 if private else 0o755, exist_ok=True)
    temporary = "%s.%d.tmp" % (file, os.getpid())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, file)


def clamp(low, value, high):
    return max(low, min(high, value))


def settings():
    """desk.json over the defaults; a value of the wrong kind is ignored."""
    given = read_json(path("desk.json"), {})

    def merged(defaults, values):
        result = dict(defaults)
        for key, default in defaults.items():
            value = values.get(key, default) if isinstance(values, dict) else default
            if isinstance(default, dict):
                result[key] = merged(default, value)
            elif type(value) is type(default) or (
                    isinstance(default, int) and not isinstance(default, bool)
                    and isinstance(value, (int, float)) and not isinstance(value, bool)):
                result[key] = value
        return result
    return merged(SETTINGS_DEFAULTS, given)


def tracking_switch():
    return read_json(os.path.join(TRACKING_DIR, "settings.json"), {}).get("switch") is not False


def tracking_on():
    return read_json(os.path.join(TRACKING_DIR, "tracking.json"), {}).get("on") is not False


def tracking_off(by, why):
    """Turns sun tracking off for everything, because this device was set by hand."""
    if tracking_on():
        write_json(os.path.join(TRACKING_DIR, "tracking.json"), {"on": False, "since": time.time(), "by": by, "why": why})


def mired_to_kelvin(mired):
    """Home's colour temperature as one sundial can set: 2700-6000K, or 6500K for no tint."""
    kelvin = int(round(1e6 / mired / 100.0)) * 100
    return KELVIN_MAX if kelvin > KELVIN_TINT_MAX else max(KELVIN_MIN, kelvin)


def kelvin_to_mired(kelvin):
    return clamp(MIRED_MIN, int(round(1e6 / clamp(KELVIN_MIN, kelvin, KELVIN_MAX))), MIRED_MAX)


def kelvin_to_rgb(kelvin):
    """Approximate colour of a white point, each part 0-1."""
    t = kelvin / 100.0
    if t <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(t) - 161.1195681661
        blue = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        red = 329.698727446 * (t - 60) ** -0.1332047592
        green = 288.1221695283 * (t - 60) ** -0.0755148492
        blue = 255.0
    return tuple(clamp(0.0, part, 255.0) / 255.0 for part in (red, green, blue))


def tint(kelvin):
    """How much of red, green and blue is left when white is warmed to `kelvin`. 6500K is all of each."""
    warm, plain = kelvin_to_rgb(clamp(1000, kelvin, KELVIN_MAX)), kelvin_to_rgb(KELVIN_MAX)
    return tuple(round(min(1.0, w / p), 3) for w, p in zip(warm, plain))


# ---------------------------------------------------------------- sundial

class Sundial:
    """The few things asked of sundial and the displays. The tests replace this."""

    def state(self):
        return read_json(SUNDIAL_STATE_FILE, {})

    def manual(self, arguments):
        """Runs `sundial manual ...`. True if it worked."""
        try:
            done = subprocess.run([SUNDIAL, "manual"] + arguments, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("sundial manual failed: %s", error)
            return False
        if done.returncode != 0:
            log.warning("sundial manual failed: %s", (done.stderr or done.stdout).strip())
        return done.returncode == 0

    def resume(self):
        try:
            subprocess.run([SUNDIAL, "resume"], capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("sundial resume failed: %s", error)

    def displays(self):
        """How many external displays answer, counted the way sundial counts them."""
        try:
            out = subprocess.run([M1DDC, "display", "list"], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return 0
        return sum(1 for line in out.splitlines() if line.startswith("["))

    def sleep_displays(self, asleep):
        command = ["/usr/bin/pmset", "displaysleepnow"] if asleep else ["/usr/bin/caffeinate", "-u", "-t", "1"]
        try:
            subprocess.run(command, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            log.warning("%s failed: %s", command[0], error)


def manual_override(state, now):
    """sundial's manual override if one is in force, else None."""
    override = state.get("override")
    try:
        if override["mode"] == "manual" and datetime.fromisoformat(override["until"]).timestamp() > now:
            return override
    except (KeyError, TypeError, ValueError):
        pass
    return None


def sun_target(state, now):
    """What the sun calls for, from sundial's state, or None if it is missing or old.

    This is sundial's "target", which is always the sun's. A manual override
    of the monitors changes only "applied".
    """
    try:
        if now - os.stat(SUNDIAL_STATE_FILE).st_mtime > SUNDIAL_STALE_SECONDS:
            return None
        target = state["target"]
        return {"daylight": clamp(0.0, float(target["daylight"]), 1.0), "kelvin": float(target["kelvin"])}
    except (OSError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- the monitors

class Monitors:
    """The external displays as one light. sundial does all the setting."""

    temperature = True

    def __init__(self, sundial, clock=time.time):
        self.sundial, self.clock = sundial, clock
        self.count, self.counted = 0, None

    @property
    def settings(self):
        return settings()["monitors"]

    def memory(self):
        return read_json(path("desk-state.json"), {}).get("monitors") or {}

    def remember(self, **changes):
        state = read_json(path("desk-state.json"), {})
        state["monitors"] = dict(state.get("monitors") or {}, **changes)
        write_json(path("desk-state.json"), state)

    def reachable(self):
        """False with no external display connected, which Home shows as "No Response"."""
        now = self.clock()
        if self.counted is None or now - self.counted >= DISPLAY_CHECK_SECONDS:
            self.count, self.counted = self.sundial.displays(), now
        return self.count > 0

    def view(self):
        """What Home is shown: what sundial last applied."""
        state, memory = self.sundial.state(), self.memory()
        applied = state.get("applied") or {}
        off = bool(memory.get("off"))
        if off and self.settings["off_means"] == "dim":
            # "Off" is a held brightness of 0. Once sundial lets go of it (the
            # hold ran out, or `sundial resume`), the monitors are on again.
            override = manual_override(state, self.clock())
            if override is None or override.get("brightness") != 0:
                off = False
                self.remember(off=False, restore=None)
        return {"on": not off, "brightness": int(clamp(0, applied.get("brightness", 0), 100)),
                "kelvin": int(clamp(KELVIN_MIN, applied.get("kelvin", KELVIN_MAX), KELVIN_MAX))}

    def set(self, values):
        """A change from Home: On, Brightness, ColorTemperature. Held by sundial, then let go."""
        memory, state = self.memory(), self.sundial.state()
        override = manual_override(state, self.clock())
        asleep = self.settings["off_means"] == "sleep"
        held = []
        if values.get("On") is False:
            if not memory.get("off"):
                # Remember what to come back to: a brightness held by hand, or the sun's.
                self.remember(off=True, restore=(override or {}).get("brightness"))
            if asleep:
                self.sundial.sleep_displays(True)
            else:
                held.append("brightness=0")
        else:
            if asleep and memory.get("off"):
                self.sundial.sleep_displays(False)
            if "Brightness" in values:
                held.append("brightness=%d" % clamp(0, int(values["Brightness"]), 100))
            elif memory.get("off") and not asleep:
                restore = memory.get("restore")
                held.append("brightness=%s" % ("sun" if restore is None else restore))
            if memory.get("off") and ("On" in values or "Brightness" in values):
                self.remember(off=False, restore=None)
        if "ColorTemperature" in values:
            held.append("kelvin=%d" % mired_to_kelvin(values["ColorTemperature"]))
        if held:  # nothing to hold (a second "on") must not replace a pause or movie mode
            minutes = FOREVER_MINUTES if tracking_switch() else self.settings["hold_minutes"]
            self.sundial.manual([str(minutes)] + held)
        if tracking_switch() and ("Brightness" in values or "ColorTemperature" in values):
            tracking_off("monitors", "set from Apple Home")

    def react(self):
        """Keeps the monitors in step with the sun tracking switch.

        Off: sundial is told to hold what the displays show now, with no end.
        On again: sundial is let go. A brightness or colour that sundial is
        holding while tracking is on was set by hand (`sundial manual`), and
        turns tracking off.
        """
        if not tracking_switch():
            return
        memory, state = self.memory(), self.sundial.state()
        override = manual_override(state, self.clock())
        applied = state.get("applied") or {}
        off = bool(memory.get("off")) and self.settings["off_means"] == "dim"
        if tracking_on():
            if memory.get("frozen"):
                self.remember(frozen=False)
                if off:
                    self.sundial.manual([str(FOREVER_MINUTES), "brightness=0", "kelvin=sun"])
                elif override is not None:
                    self.sundial.resume()
            elif override is not None and ("kelvin" in override or ("brightness" in override and not off)):
                tracking_off("monitors", "brightness %s, %sK" % (applied.get("brightness", "?"), applied.get("kelvin", "?")))
        elif not memory.get("frozen") and "brightness" in applied and "kelvin" in applied:
            held = ["kelvin=%d" % applied["kelvin"]] + ([] if off else ["brightness=%d" % applied["brightness"]])
            self.sundial.manual([str(FOREVER_MINUTES)] + held)
            self.remember(frozen=True)

    def describe(self):
        view, state = self.view(), self.sundial.state()
        override = state.get("override") if isinstance(state.get("override"), dict) else None
        text = "%s, brightness %d, %dK" % ("on" if view["on"] else "off", view["brightness"], view["kelvin"])
        if not self.reachable():
            return "no external display connected"
        if override:
            text += " (sundial %s until %s)" % (override.get("mode"), str(override.get("until", ""))[11:16])
        else:
            text += " (following the sun)"
        return text


# ---------------------------------------------------------------- the keyboard

class Keyboard:
    """The keyboard's lighting. wooting-workspaces does all the setting; this writes its control file."""

    temperature = True

    def __init__(self, sundial, clock=time.time):
        self.sundial, self.clock = sundial, clock
        self.followed = {"brightness": 100, "kelvin": KELVIN_MAX}  # the last the sun was known to ask for

    @property
    def settings(self):
        return settings()["keyboard"]

    def override(self):
        """The change from Home still in force, or {}."""
        override = read_json(path("desk-state.json"), {}).get("keyboard") or {}
        return override if override.get("until", 0) > self.clock() else {}

    def reachable(self):
        return True  # the keyboard cannot be asked; the control file is always there to write

    def view(self):
        """What the keyboard should be showing now: the sun's wish, with Home's on top."""
        chosen = self.settings
        sun = sun_target(self.sundial.state(), self.clock())
        if sun is not None:
            low, high = chosen["night_brightness"], chosen["day_brightness"]
            self.followed = {"brightness": int(round(low + (high - low) * sun["daylight"])),
                             "kelvin": int(clamp(KELVIN_MIN, round(sun["kelvin"] / 100.0) * 100, KELVIN_MAX))}
        override = self.override()
        return {"on": bool(override.get("on", True)),
                "brightness": int(clamp(0, override.get("brightness", self.followed["brightness"]), 100)),
                "kelvin": int(clamp(KELVIN_MIN, override.get("kelvin", self.followed["kelvin"]), KELVIN_MAX))}

    def control(self, view):
        """The control file's text for a view."""
        chosen = self.settings
        plain = view["on"] and view["brightness"] >= 100 and view["kelvin"] >= KELVIN_MAX
        base = "profile" if plain and chosen["keep_profile_at_full"] else chosen["base_colour"]
        return "on %d\nbrightness %.3f\ntint %.3f %.3f %.3f\nbase %s\n" % (
            (1 if view["on"] else 0, view["brightness"] / 100.0) + tint(view["kelvin"]) + (base,))

    def sync(self):
        """Writes the control file if what it should say has changed. Returns the view."""
        view = self.view()
        text = self.control(view)
        try:
            with open(CONTROL_FILE) as handle:
                same = handle.read() == text
        except OSError:
            same = False
        if not same:
            os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
            temporary = "%s.%d.tmp" % (CONTROL_FILE, os.getpid())
            with open(temporary, "w") as handle:
                handle.write(text)
            os.replace(temporary, CONTROL_FILE)
        return view

    def set(self, values):
        """A change from Home: On, Brightness, ColorTemperature. Held, then the sun takes over again."""
        override = {key: value for key, value in self.override().items() if key != "until"}
        if "On" in values:
            override["on"] = bool(values["On"])
        if "Brightness" in values:
            override["brightness"] = clamp(0, int(values["Brightness"]), 100)
        if "ColorTemperature" in values:
            override["kelvin"] = clamp(KELVIN_MIN, int(round(1e6 / values["ColorTemperature"] / 100.0)) * 100, KELVIN_MAX)
        override["until"] = FOREVER if tracking_switch() else self.clock() + self.settings["hold_minutes"] * 60
        self.hold(override)
        if tracking_switch() and ("Brightness" in values or "ColorTemperature" in values):
            tracking_off("keyboard", "set from Apple Home")
        self.sync()

    def hold(self, override):
        state = read_json(path("desk-state.json"), {})
        state["keyboard"] = override
        write_json(path("desk-state.json"), state)

    def react(self):
        """Keeps the keyboard in step with the sun tracking switch.

        Off: what the keyboard shows now is held, with no end. On again: it
        is let go, except that a keyboard switched off stays off.
        """
        if not tracking_switch():
            return
        override = self.override()
        if tracking_on():
            if "brightness" in override or "kelvin" in override:
                self.hold({"on": False, "until": FOREVER} if override.get("on") is False else {})
        elif not override.get("frozen"):
            self.hold(dict(self.view(), frozen=True, until=FOREVER))

    def describe(self):
        view, override = self.sync(), self.override()
        text = "%s, brightness %d%%, %dK" % ("on" if view["on"] else "off", view["brightness"], view["kelvin"])
        if override:
            return text + " (set from Home; following the sun again at %s)" % time.strftime(
                "%H:%M", time.localtime(override["until"]))
        return text + " (following the sun)"


# ---------------------------------------------------------------- pairing record

def data_dir():
    return path("homekit-data")


def pairing_file():
    return os.path.join(data_dir(), "pairing.json")


def load_or_create_pairing():
    pairing = read_json(pairing_file(), {})
    if not pairing.get("setup_code") or not pairing.get("setup_id"):
        while True:
            digits = "".join(str(secrets.randbelow(10)) for _ in range(8))
            if digits not in WEAK_CODES:
                break
        pairing = {
            "setup_code": "%s-%s-%s" % (digits[:3], digits[3:5], digits[5:]),
            "setup_id": "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(4)),
        }
        write_json(pairing_file(), pairing, private=True)
    return pairing


# ---------------------------------------------------------------- Apple Home

def home_values(view):
    """Home's characteristics for a view."""
    return {"On": bool(view["on"]), "Brightness": int(view["brightness"]),
            "ColorTemperature": kelvin_to_mired(view["kelvin"])}


class Link:
    """Hands Home's changes to a device from one worker thread.

    Home sends a burst of writes while a slider moves, and setting the
    monitors takes a second or more. The newest value of each kind waits its
    turn, so a burst collapses instead of queueing up.
    """

    def __init__(self, device):
        self.device = device
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.waiting = {}
        self.stopping = False
        self.thread = threading.Thread(target=self.run, name="desk-sender", daemon=True)

    def write(self, values):
        with self.lock:
            self.waiting.update(values)
        self.wake.set()

    def busy(self):
        with self.lock:
            return bool(self.waiting)

    def flush(self):
        with self.lock:
            values = dict(self.waiting)
        if values:
            try:
                self.device.set(values)
            except Exception as error:  # noqa: BLE001 - the worker must outlive one bad change
                log.warning("could not set %s: %r", type(self.device).__name__.lower(), error)
        with self.lock:
            # Anything written during the send is still waiting.
            self.waiting = {key: value for key, value in self.waiting.items() if values.get(key) != value}

    def run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            if self.stopping:
                return
            self.flush()

    def stop(self):
        self.stopping = True
        self.wake.set()


def build_bridge(driver, chosen, devices):
    from pyhap.accessory import Accessory, Bridge
    from pyhap.const import CATEGORY_LIGHTBULB

    class DeskLight(Accessory):
        category = CATEGORY_LIGHTBULB

        def __init__(self, name, link, model):
            super().__init__(driver, name)
            self.link, self.device = link, link.device
            self.set_info_service(firmware_revision=VERSION, manufacturer="Desk", model=model,
                                  serial_number="DESK-" + model.upper()[:8])
            service = self.add_preload_service("Lightbulb", chars=["Brightness", "ColorTemperature"])
            self.chars = {
                "On": service.configure_char("On"),
                "Brightness": service.configure_char("Brightness"),
                "ColorTemperature": service.configure_char(
                    "ColorTemperature", properties={"minValue": MIRED_MIN, "maxValue": MIRED_MAX}),
            }
            service.setter_callback = self.home_wrote
            self.reachable = self.device.reachable()
            self.show(notify=False)

        @property
        def available(self):
            # False makes Home show "No Response" instead of a state it cannot change.
            return self.reachable

        def show(self, notify, skip=()):
            for label, value in home_values(self.device.view()).items():
                if label not in skip and self.chars[label].get_value() != value:
                    self.chars[label].set_value(value, should_notify=notify)

        def home_wrote(self, values):
            if not self.reachable:
                raise RuntimeError("nothing to set")
            self.link.write(values)

        @Accessory.run_at_interval(POLL_SECONDS)
        def run(self):
            self.reachable = self.device.reachable()
            if not self.link.busy():
                self.device.react()
            if hasattr(self.device, "sync"):
                self.device.sync()
            if not self.link.busy():  # do not show an old value over one Home has just written
                self.show(notify=True)

        def stop(self):
            self.link.stop()

    class Desk(Bridge):
        def setup_message(self):
            # The default prints the setup code and a QR code into the log.
            log.info("not paired yet; run this file with --pairing to see the setup code")

    bridge = Desk(driver, chosen["name"])
    bridge.set_info_service(firmware_revision=VERSION, manufacturer="Desk", model="Desk bridge", serial_number="DESK-0001")
    for key, device in devices:
        link = Link(device)
        link.thread.start()
        bridge.add_accessory(DeskLight(chosen[key]["name"], link, key))
    return bridge


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
    pairing = read_json(pairing_file(), {})
    pairing.update({
        "uri": accessory.xhm_uri(),
        "paired": bool(driver.state.paired),
        "name": accessory.display_name,
        "address": driver.state.address,
        "port": driver.state.port,
    })
    write_json(pairing_file(), pairing, private=True)


# ---------------------------------------------------------------- entry points

def serve():
    from pyhap import util
    from pyhap.accessory_driver import AccessoryDriver

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", stream=sys.stdout)
    os.makedirs(data_dir(), mode=0o700, exist_ok=True)
    os.chmod(data_dir(), 0o700)
    chosen = settings()
    pairing = load_or_create_pairing()
    sundial = Sundial()
    devices = [("monitors", Monitors(sundial)), ("keyboard", Keyboard(sundial))]

    address = util.get_local_address()
    driver = AccessoryDriver(
        address=address, port=int(chosen["port"]), persist_file=os.path.join(data_dir(), "accessory.state"),
        pincode=pairing["setup_code"].encode("ascii"),
        # Announce on the home network only, not on VPN or tunnel interfaces.
        interface_choice=[address],
    )
    driver.state.setup_id = pairing["setup_id"]  # keeps the QR code valid across restarts
    log_home_writes(driver)
    bridge = build_bridge(driver, chosen, devices)
    driver.add_accessory(bridge)
    record_pairing(driver, bridge)

    def watch():
        """Notes a pairing in the record, and restarts if the network address changes."""
        was_paired = driver.state.paired
        while True:
            time.sleep(60)
            if driver.state.paired != was_paired:
                was_paired = driver.state.paired
                log.info("now %s", "paired with Apple Home" if was_paired else "unpaired")
                record_pairing(driver, bridge)
            try:
                if util.get_local_address() != address:
                    # Home finds the bridge by the address announced at start.
                    log.info("network address changed; restarting to announce it")
                    os.kill(os.getpid(), signal.SIGTERM)
            except OSError:
                pass
    threading.Thread(target=watch, name="desk-watch", daemon=True).start()

    signal.signal(signal.SIGTERM, driver.signal_handler)
    log.info("Desk HomeKit %s: '%s' (%s) on %s:%d, %s", VERSION, chosen["name"],
             ", ".join(chosen[key]["name"] for key, _ in devices), address, int(chosen["port"]),
             "paired" if driver.state.paired else "waiting to be paired")
    driver.start()
    return 0


def show_pairing():
    pairing = read_json(pairing_file(), {})
    if not pairing.get("setup_code"):
        print("desk-homekit: the service has not run yet, so there is no setup code", file=sys.stderr)
        return 1
    print(json.dumps(pairing, indent=2, sort_keys=True))
    return 0


def write_qr(file):
    pairing = read_json(pairing_file(), {})
    if not pairing.get("uri"):
        print("desk-homekit: the service has not run yet, so there is no pairing code", file=sys.stderr)
        return 1
    import pyqrcode
    pyqrcode.create(pairing["uri"], error="M").svg(file, scale=10, quiet_zone=4, background="white")
    os.chmod(file, 0o600)  # the code lets anyone on the network pair
    print(file)
    return 0


def show_status():
    sundial = Sundial()
    print("Monitors: %s" % Monitors(sundial).describe())
    print("Keyboard: %s" % Keyboard(sundial).describe())
    return 0


def main(argv):
    if len(argv) == 2 and argv[1] == "--pairing":
        return show_pairing()
    if len(argv) == 3 and argv[1] == "--qr":
        return write_qr(argv[2])
    if len(argv) == 2 and argv[1] == "--status":
        return show_status()
    if len(argv) == 1:
        return serve()
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
