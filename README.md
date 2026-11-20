# Neewer Bridge

Controls a Neewer Bluetooth light from a Mac, with no window and no vendor app. The light now is a NEEWER AS600B, a 600 W bi-colour COB light. It was first written for a CB200C, and that light is still supported (see Light profiles).

It has three parts:

- **The bridge** (`bridge/main.swift`) is a background program that holds the macOS Bluetooth permission. It finds the light, stays connected, and writes whatever bytes it is given. It listens on `127.0.0.1:18490` only.
- **The `neewer` command** (`cli/neewer`) builds the light's command frames and hands them to the bridge. The whole command format lives in this one file.
- **The Apple Home service** (`homekit/`) is optional. It presents the light to Apple Home as one colour bulb and sends its commands through the bridge.

A second light lives in the same repository: `fixture/` drives the WiZ bulbs of a ceiling fixture as one light. It has [its own section](#the-ceiling-fixture-wiz-bulbs-as-one-light) below.

The split is deliberate. Rebuilding the bridge changes its code signature, and macOS then asks for Bluetooth permission again. Changing the `neewer` script never does.

## Use

```
neewer on
neewer off
neewer white 5600 10        # colour temperature in kelvin, brightness 0-100 (below)
neewer white 2700 5 --output  # brightness as light output, a percent of full
neewer white 3200 --tint -10
neewer brightness 20.5      # keeps the current colour
neewer sunrise --minutes 30 # ramp up the way a real sunrise brightens
neewer follow               # show whether the light is following the sun
neewer status
```

On the AS600B a brightness set by hand is the light's own setting on its exponential curve, the number on its dial: 0.1 to 100.0 in steps of 0.1. That keeps control fine at the low end, where exp 20.0 is 1.3% of full output (see Brightness on the AS600B). `--output` takes light output instead, as a percent of full, fractions allowed. On the CB200C the two are the same, a whole number from 1. Colour temperature is 2700-6500K on the AS600B, in steps of 100K, as on its display.

These only work on a light that has colour LEDs or takes the five-channel frame, such as the CB200C. On the AS600B each says so and sends nothing (`colour` and `rgb` also name the nearest white):

```
neewer colour 240 100 10    # hue 0-360, saturation 0-100, brightness
neewer rgb ff8800 10
neewer faint 5              # dimmer than 1%: level 1 (dimmest) to 255
neewer channels 1 --warm 20 # drive the five LED channels directly, 0-255 each
```

`neewer status` shows the light's profile and what the command last sent, not what the light is doing: the light does not report its state, so changes made on its own dial are not seen.

## Light profiles

What the connected light can do is its profile, in `light.json` in the settings folder:

```
{
  "mac": "F0:6F:33:D3:09:34",
  "profile": {"model": "AS600B"}
}
```

`model` picks one of the profiles built into `cli/neewer`, and any field given beside it overrides that profile's own. Without a profile the light is taken to be a CB200C.

| | AS600B | CB200C |
|---|---|---|
| `kelvin_min`, `kelvin_max` | 2700, 6500 | 2500, 7500 |
| `curve` | `exp` | none: each command chooses, linear unless asked |
| `channels` (the five-channel frame) | false | true |
| `colour` (the hue frame) | false | true |
| `tenths` (the brightness's tenths in the white frame) | true | false |
| `firmware_curves` (dims on the firmware's curves, below) | true | false |
| `dial` (a brightness set by hand is the setting, not output) | true | false |

Every white frame, from every path (the command, the follower, the bedtime fade, the morning, a fresh start, the sunrise, Apple Home), is kept inside the profile's colour temperatures and carries its curve. This matters on the AS600B, which:

- **drops a white frame outside 2700-6500K.** 2500K and 6600K were tried; its display kept the old value. So nothing outside the range is sent: the follower and the rest bring a colour into the range, and `neewer white 6600` refuses with the range.
- **needs a fifth value in the white frame**, the tenths of the brightness. Without it the light reads the tenths as 255, so every brightness arrives 25.5 too high, and one that comes out above 100.0 is dropped (see Command format).
- **honours the curve byte**, which sets the light's own dimming curve: 04 linear, 01 exponential. It is kept on the exponential curve (below).
- **ignores the channels frame altogether** (cold white at 255, brightness 15%, did nothing), so there is no `faint`, no `channels`, and a different sunrise (below).

### Brightness on the AS600B

The AS600B's setting goes from 0.0 to 100.0 in steps of 0.1, as on its dial, and the light is kept on its exponential curve. The curves are the firmware's own, worked out from the firmware of two siblings, the AS600C and the bi-colour FS600B. For a setting n, as a share of full output (linear 100):

```
linear       n / 100
exponential  n / 100 × 1.035^(n − 100)
```

The log and S curves are in `cli/neewer` too. The exponential curve reaches full output at 100.

| exp setting | light output |
|---|---|
| 0.1, the lowest | 0.003% |
| 17.2 | 1% |
| 20.0 | 1.3% |
| 39.7 | 5% |
| 52.0 | 10% |
| 84.7 | 50% |
| 100.0 | 100% |

- **By hand** (`neewer white`, `neewer brightness`, the Apple Home slider), brightness is the setting, so control is fine at the low end. `--output` on the command takes light output instead.
- **The follower, the bedtime fade, the morning, a fresh start and the sunrise work in light output.** They send the setting, to 0.1, whose output comes nearest. So `night_brightness: 1.28` in `follow.json` is exp 20.0.
- `neewer status` and `neewer follow` show both: `white 2700K at exp 20.0 (1.3% output)`.

The formula has not been measured on this light. Matches made by eye before the tenths byte was found agree with it to within 10-16%, once corrected for the 25.5: linear 30.5 looked like exp 70.5, and linear 45.5 like exp 80.5.

**A change to the exponential curve never flashes.** Only a raw send (`--curve`) leaves the light on another curve. When the curve byte changes, the light first shows the new number on its old curve and changes curve a moment later. The exponential curve is the dimmest, so the next ordinary send dips first. It sends an exponential frame with the number the light shows now, waits `switch_pause` (1.5 seconds), then sends the target. When the light's curve is not known, the first frame is exp 0.1.

`neewer white 5600 45.5 --curve linear` (or `--curve exp`) sends the number raw on that curve, to one decimal, for comparisons by eye. No care is taken over the change of curve.

A light without firmware curves (the CB200C) has one curve and whole-number settings. An optional `output_curve` of setting and share pairs says how bright each setting really is.

## Sunrise

`neewer sunrise` runs one ramp and exits when it reaches the top:

```
neewer sunrise --minutes 30                       # ends at 100%, 5600K
neewer sunrise --minutes 20 --brightness 60 --kelvin 5000
neewer sunrise --minutes 30 --plan                # print the steps, send nothing
```

The brightness follows the real sky. It uses the standard table of clear-sky illuminance against the sun's elevation: about 3.4 lux when the sun is 6 degrees below the horizon (the start of civil twilight), about 450 lux at sunrise, and about 10,900 lux with the sun 10 degrees up. The sun climbs at a steady rate through the ramp, as it very nearly does around sunrise, and at each moment the light is as bright, relative to its target, as the sky is relative to the sky at 10 degrees. So the ratios are the real ones, squeezed into the time you give it:

- It starts where the sky is as dim as the light's faintest level, with the sun about 7 degrees down. Roughly the first third of the ramp is twilight below 1%.
- Brightness climbs fastest just before the sun comes up, then eases, as it does outside.
- With the default target the light passes 1% a third of the way in, and sunrise itself, at about 4%, falls near 40%.

The colour is an approximation, not a measured table. It is a view towards the sunrise: cold white in twilight, warming to about 2800K as the sun reaches the horizon, then whitening to the target.

**On the AS600B** there is no channels frame, so the sky sunrise is sent as white frames only. It is the same sky in light output: `--brightness` is where it ends, as light output, and each step sends the exp setting, to 0.1, that gives the sky's share (see Brightness on the AS600B). It starts where the sky is as dim, relative to the end, as the light's lowest setting is: exp 0.1, about 0.003%, in deep twilight. It climbs on the exponential curve all the way, a tenth of the setting at a time. The colour is the sky's, kept within 2700-6500K: cool white in twilight, about 2800K at the horizon, then whitening to the target. `--style glow` on this light is a plain ramp in light output from 0.003% to the target, warmest white to the target colour. Neither has been watched on the light yet. `--plan` shows the steps, each with its setting and output.

**On the CB200C** the whole ramp is sent as channels frames, mixing the warm and cold white LEDs, and never uses the white frame. Switching between the two frames shows a wrong colour for a moment, and the channels frame takes fractions of a percent all the way up. The mix was matched by eye against the white frame at 2800K, at 1% and at 10%: channels brightness `b` with levels adding up to 176 is as bright as white at `b`%, 2800K is about 95% warm and 5% cold, and about 1 part in 60 of red takes off a green cast. It has not been compared at other colour temperatures or near full brightness.

`neewer sunrise --style glow` is the older, designed ramp: a red glow, then warm white, then white, each climbing by a constant ratio.

Setting the light any other way, from Apple Home or with another `neewer` command, stops the ramp. When it finishes on the CB200C, the light is left in the channels mode, which Apple Home shows as white.

A sunrise usually runs with nobody watching, so:

- It always switches the light on, whatever the last setting says, in case the light was put in standby on its own button. The first step is sent before the power frame as well as after it, so a light that keeps a new setting while in standby wakes as a faint glow instead of at its old setting. Whether the CB200C keeps it has not been checked.
- If the light drops out, the ramp waits for it and carries on from wherever it should be by then, switching the light on again first. It gives up 5 minutes after the ramp should have ended.

To start one at a set time, use the Sunrise switch in Apple Home (below).

## Following the sun

`./install.sh follow` makes the light follow the sun through the day. Once a minute it sets brightness and colour temperature from the sun's height: full and cool white by day, dimming and warming through sunset, then low and warm all night.

It does not work out the sun's position itself. It reads the file that sundial writes every minute (`~/.local/state/sundial/state.json`). sundial is a separate program, not part of this project, that does the same job for the monitors. So the light and the screens move together and the location is set in one place. Without sundial running, the light is left alone.

```
neewer follow            # show what it is doing and why
neewer follow off        # pause it; the light stays as it is
neewer follow on         # start again (Sun Tracking is its own switch: neewer tracking on)
```

The rules:

- **It switches the light off and on only at bedtime and in the morning** (below). Otherwise it never does, and while the light is off it does nothing.
- **A setting made elsewhere wins.** If you set the light from Apple Home or with another `neewer` command, sun tracking turns off and the light is left alone until it is turned on again (see Sun Tracking below), with `neewer tracking on` or the Sun Tracking switch.
- **Switched on by day, it starts afresh** (below): it goes to the sun's setting, not back to how it was left.
- **It keeps out of a sunrise.** Nothing is sent while `neewer sunrise` runs. When it ends, following takes the light over from where the sunrise left it; a sunrise does not turn sun tracking off. (In the older way without the switch, what it leaves is held for two hours like any other setting.)
- **It only sends when the target changes.** That is about once a minute around sunrise and sunset, and not at all at midday or at night.
- If the bridge or the light cannot be reached, it tries again each minute and writes one line to the log, not one a minute.

The settings are in `follow.json` in the settings folder, and are read each minute:

```
{
  "enabled": true,
  "day_brightness": 100,
  "night_brightness": 5,
  "night_kelvin": 2700,
  "hold_minutes": 120,
  "fresh_start": true,
  "bedtime_rule": true,
  "morning_on": true,
  "fade_minutes": 5,
  "morning_lead_minutes": 30,
  "bedtime": "",
  "wake": ""
}
```

- Brightness runs from `night_brightness` to `day_brightness` with sundial's "daylight" share. Both are light output as a percent of full, and may be fractions on the AS600B. The setting sent is the one that gives it: on the AS600B, 5% is exp 39.7, 1.28% is exp 20.0 and 100% is exp 100.0. With sundial's defaults that is the night level until the sun is 6 degrees below the horizon and the day level once it is 20 degrees up.
- Colour is sundial's colour, kept within what the light shows (2700-6500K on the AS600B). While the sun goes from the horizon to 6 degrees below, it moves to `night_kelvin`.
- `night_brightness` cannot go below the light's lowest setting, because 0 would switch the light off. On the CB200C that is 1, a whole percent. On the AS600B it is exp 0.1, about 0.003%.
- `hold_minutes` is how long a setting made elsewhere is left alone, in the older way without the Sun Tracking switch. With the switch it is not used.

### A fresh start

A light that was off and is switched on again by day goes straight to the sun's setting and is followed from there. Without this it would come back as it was left, last night's warm white in the middle of the morning, and the first touch of the brightness would hold that for two hours.

- It applies whenever the follower has seen the light off, however it was switched off and however it is switched on.
- A new day begins afresh too. A light that is on when the morning time comes (half an hour before waking) is set from the sun then, even if it was set by hand during the night.
- A brightness changed in the same minute as the switching on is not kept: the fresh start comes first. After it, a setting made by hand is held for two hours as ever.
- **How quickly.** Switched on from Apple Home, the light comes on at the sun's setting at once: the Home service asks for it as it switches the light on. Switched on any other way (`neewer on`, the light's own button), it shows the old setting until the follower's next look, up to a minute.
- **Not at night.** Between bedtime and the morning a light switched on by hand is left exactly as it comes on, as the bedtime rule says.
- `"fresh_start": false` in `follow.json` goes back to the old way. `neewer follow` has a line for it: `due: when the light is switched on it goes straight to the sun's setting`, `made at 10:58`.

### Bedtime

At bedtime the light fades out over five minutes and switches off. Half an hour before the wake time it switches on again, at whatever the sun calls for at that moment, and follows the dawn up.

Bedtime and the wake time are sundial's (`[sleep]` in `~/.config/sundial/config.ini`, 23:00 and 07:00 as it stands). They are read from there every minute, not copied, so changing them in sundial changes them here. `"bedtime"` and `"wake"` in `follow.json` (as `"22:30"`) take their place if set.

- **Once a night.** The light is switched off once, at bedtime. If anything switches it back on before morning, or sets it, it is left alone until morning: it is not switched off a second time, and it is not followed either. If it is already off at bedtime, nothing is done.
- **On in the morning only if the follower switched it off.** If you switched it off yourself, at any point, it stays off.
- **A setting made by hand does not put bedtime off.** A light set an hour before bedtime still goes off at bedtime.
- **A setting made during the fade stops the fade**, and the light is yours for the night.
- **Only near the time.** If the Mac was asleep at bedtime, or the rule was switched on in the middle of the night, that night is left alone. Bedtime is acted on within 15 minutes of it.
- Switching on by hand after the fade brings back the setting from before the fade, not its last step. From Apple Home and the `neewer` command that is certain. The light's own button may wake it at its faintest: the old setting is sent to the light as it is switched off, but whether it keeps a setting sent in standby is not known.
- A `neewer sunrise` running at bedtime or at the morning time is left to itself.

`neewer follow` says what the rule will do next: `off at 23:00, on again at 06:30`, `off for the night; on at 06:30`, `on by hand since 01:10; left alone tonight`.

`"bedtime_rule": false` switches the whole rule off. `"morning_on": false` keeps the switching off and drops the switching on. `fade_minutes` and `morning_lead_minutes` are the five minutes and the half hour.

It uses the ordinary white frame, one step a second, each the same share dimmer. On the AS600B the fade stays on the exponential curve. From 5% that is about 2.4% less light a step, a few tenths of the setting; from 1.28%, about 2%. Below about exp 5 (0.2%) each step is the least the light has, 0.1, down to exp 0.1. The light's curve stays known through the fade, so the morning comes on in one frame. On the CB200C, after a sunrise the light is in the channels mode, so there is one brief wrong colour when following takes over.

Each change goes to `~/Library/Logs/neewer-follow.log`. `python3 tests/test_follow.py` checks the rules without a light.

## Apple Home

`./install.sh homekit` installs the service. It runs [HAP-python](https://github.com/ikalchev/HAP-python) in its own Python environment, under a small launcher app (`NeewerHomeKit.app`) so macOS has one named program to attach the local-network permission to.

To pair, open the Home app, choose Add Accessory, and scan the QR code or enter the setup code. Home will warn that the accessory is uncertified; that is expected. Show the code with:

```
"$HOME/Library/Application Support/NeewerBridge/venv/bin/python" \
  "$HOME/Library/Application Support/NeewerBridge/homekit/neewer_homekit.py" --pairing
```

Add `--qr code.svg` in place of `--pairing` to write the QR code to a file.

- Home shows the light as on/off, brightness, colour and colour temperature, the range being the light's profile's (2700-6500K on the AS600B: 154-370 mired). The model shown is the profile's.
- **On a light with white LEDs only (the AS600B)**, a colour chosen on Home's colour wheel becomes the nearest white the light can show, and Home is then shown that white. No colour frame is ever sent. The colour controls are still there, because taking them away would mean pairing the light again.
- **On the AS600B the brightness slider is the light's setting**, the number on its dial: 20 is exp 20.0, 1.3% of full output, and 1 is exp 1.0, 0.03%. So the low end of the slider is fine-grained. Home is shown the setting, rounded to a whole number and never below 1 while the light is on. Below exp 1 is `neewer brightness 0.5`. On the CB200C the slider is light output.
- Home and the `neewer` command share one record of the last setting, so a change made with either shows up in the other within a few seconds.
- If the bridge loses the light, Home shows "No Response" rather than a state it cannot change.
- The pairing keys are in `homekit-data/` in the settings folder, readable only by you. Deleting that folder unpairs the light; remove it from the Home app first.
- `homekit.json` in the settings folder takes `name`, `port` (default 51826) and `sunrise` (below).

### Sunrise switch

The accessory has a second tile, a switch named Sunrise. Turning it on runs `neewer sunrise`; it turns itself off when the ramp ends, and turning it off stops the ramp. Any change to the light from Home also stops it.

To wake up to it, make an automation in the Home app: Automation, then A Time of Day, pick the time the ramp should start (30 minutes before the alarm, with the default ramp), and choose the Sunrise switch, turned on. Time-of-day automations need a home hub (an Apple TV or HomePod), and the Mac has to be awake when it fires.

The ramp is set in `homekit.json`; leave out any value to use the command's default:

```
{
  "sunrise": {"minutes": 30, "brightness": 100, "kelvin": 5600, "style": "sky"}
}
```

Restart the service after changing it: `launchctl kickstart -k gui/$(id -u)/local.neewer-homekit`. A value the command would reject is reported in `neewer-homekit.log` when the service starts, and the defaults are used. The ramp's progress goes to the same log.

## Sun Tracking: one switch for everything

Four things here follow the sun: the Neewer light, the ceiling fixture, the monitors and the keyboard. One switch says whether they do. It is a tile in Apple Home, **Sun Tracking**, on the fixture's accessory ("The Sun"), and a command:

```
fixture tracking          # how it stands, and why
fixture tracking on       # everything follows the sun again, at once
fixture tracking off      # everything stays as it is
```

(`neewer tracking` is the same command.)

- **On:** everything follows the sun.
- **Set any one of them by hand and it turns itself off, for all of them.** A colour or a brightness, from Apple Home, a command, the WiZ app, a bulb's own tile, `sundial manual`: any of these. Then every device stays exactly as it is. There is no timer; nothing takes over after two hours. One switch means one state, so a brightness set on the keyboard stops the lights following too.
- **Switching a device on or off is not setting it.** Tracking stays on.
- **Turn it on and everything goes to the sun's setting at once**, within a few seconds, including whatever was set by hand. It does not switch on a light that is off. For the fixture, turning it on wins: the follower takes the fixture over first, whatever the bulbs look like then, and only a change made after that counts. Until the follower has done so, only a setting made deliberately (from Apple Home or a command) turns tracking off again, so a scene's colours still win over the scene's "on".
- **It turns itself on before sunrise every day**: at civil dawn (the sun 6 degrees below the horizon, worked out for sundial's location, about half an hour before sunrise) or at the followers' morning time (half an hour before waking), whichever comes first. Only if it was turned off before then: turned off later in the day, it stays off until the next morning.

How the other rules sit with it:

- **Bedtime is not tracking.** The lights still fade out and go off at bedtime, and come on in the morning, whatever the switch says. By the morning time the switch is on again anyway.
- **A fresh start does not turn tracking on.** With tracking off, a light switched on by day still comes on at the sun's setting, once, and is then left there. If you turned tracking off, switching a light on does not turn it back on behind your back.
- **While it is off, the monitors and the keyboard are held too.** sundial is told to hold what the displays show now (`sundial manual` with no end), and let go when tracking comes back on. sundial's own `pause` and `movie` are separate and are left alone.
- **Monitors or keyboard switched off stay off** when tracking comes on.

Scenes in Apple Home work as you would hope. A scene that sets colours turns tracking off as it runs. A scene that turns the switch on resumes it. **A scene that does both ends with its colours and tracking off**: Home sends a scene's parts in no particular order, so for five seconds after a device is set by hand, Home's "on" for the switch is not taken. (Tap the switch again after that and it is.)

How quickly a change by hand is noticed: at once from Apple Home and the commands; within about 5 seconds for the WiZ app, a bulb's own tile or `sundial manual`; within a minute for the Neewer's own dial settings made through anything else that writes its state.

The state is one file, `tracking.json` in `~/Library/Application Support/SunTracking/`, which every follower and every Apple Home service reads. `settings.json` beside it takes:

```
{"switch": true, "on_before_sunrise": true}
```

`"switch": false` goes back to the older way, in which a setting made by hand is held for `hold_minutes` (two hours) and then followed again, device by device; that is the only time `hold_minutes` is used now. `"on_before_sunrise": false` stops the switch turning itself on. Each `follow` status has a Tracking line: `on since 06:14, switched on before sunrise`, `off since 12:30, when the fixture was set by hand (white 3000K at 40%)`.

## The ceiling fixture (WiZ bulbs as one light)

`fixture/` is a second, separate light: several WiZ bulbs in one ceiling fitting, driven as one. It shares nothing with the Neewer light except the Python environment. `./install.sh fixture` installs all of it: the `fixture` command, an Apple Home accessory named "Fixture", and a sun follower.

It talks to the bulbs directly on the home network (the WiZ local protocol, JSON over UDP port 38899). No cloud and no WiZ app are involved. The bulbs can stay paired to Apple Home one by one as well; the two do not interfere.

```
fixture on
fixture off
fixture white 2700 40       # colour temperature in kelvin, brightness up to 100
fixture colour 240 100 40   # hue 0-360, saturation 0-100, brightness
fixture brightness 12.5     # keeps the colour; fractions are allowed
fixture brightness 0.4      # below 1%: every bulb still lit, on its raw white LEDs
fixture sweep 1.2 0.8 --seconds 60   # move slowly between two brightnesses
fixture compare --bulb "upper 3" --raw-w 42   # set one bulb by hand; --restore puts it back
fixture status              # asks every bulb what it is doing
fixture discover            # lists the WiZ bulbs on the network and their limits
fixture fade                # how long the bulbs themselves take to change
fixture follow              # show whether the fixture is following the sun
```

Every change goes to all the bulbs in one burst. A bulb that does not acknowledge is sent it again, up to four times in all over 3.5 seconds, and any bulb still silent is named. It is then sent what it missed as soon as it answers again.

The bulbs save power between Wi-Fi beacons, so each one acts on a change anywhere from a tenth of a second to two seconds after it is sent. They do not all change at the same instant. That is the bulbs, not this program.

### The bulbs' own fade

Each bulb fades to a new setting over a time saved in the bulb: 450 ms going brighter and 500 ms going dimmer, as they came. `fixture fade` shows it. `fixture fade 100` tries to change it and reads it back, but these bulbs (firmware 1.33.39) refuse the change over the local network with "Invalid Request", in every form tried, and stay at 450 and 500. It can only be changed in the WiZ app, which these bulbs are not joined to.

### Which bulbs

`fixture.json` in the settings folder lists the bulbs by MAC address. `fixture discover --save` writes it from every WiZ bulb it finds, with the limits the bulbs report; the installer does this the first time. Edit the list if some of the bulbs belong elsewhere.

Bulbs are known by MAC address, never by network address. Their addresses are remembered in `addresses.json` and looked for again whenever a bulb stops answering, so the router may move them.

```
{
  "name": "Fixture",
  "port": 51827,
  "bulbs": ["a1b2c3d4e5f6", "..."],
  "rings": {"upper": ["...six, clockwise..."], "lower": ["...six, clockwise..."]},
  "order": ["...all twelve, clockwise round the fixture..."],
  "layout_mapped": true,
  "power_on_look": {"temp": 4207, "dimming": 100},
  "min_dimming": 1,
  "min_colour_dimming": 10,
  "kelvin_min": 2200,
  "kelvin_max": 6500,
  "white_mode": "hybrid",
  "raw_calibration": [
    {"kelvin": 2700, "percent": 1, "c": 0, "w": 42, "dimming": 10}
  ]
}
```

These are the values for the ESP25_SHRGB_01 bulbs (firmware 1.33.39), checked on the bulbs themselves: white takes 2200-6500K and dimming 1-100, and a colour is never dimmer than 10 whatever is asked. Dimming 1 was accepted and read back; nobody has yet judged by eye how much dimmer it is than 10.

### Where the bulbs are

The fixture is twelve bulbs in two rings of six, upper and lower. The rings are offset, so going round the fixture the bulbs alternate: upper 1, lower 2, upper 2, lower 3, and so on to upper 6, lower 1, about 30 degrees apart.

Two settings in `fixture.json` say so:

- `rings` names the bulbs: each ring's MAC addresses in order, clockwise. `fixture status` then shows "upper 3", "lower 5", and `fixture compare --bulb "upper 3"` takes the same names.
- `order` is every bulb, clockwise round the fixture, taken as evenly spaced on one circle. This is what the patterns below are chosen from.

`layout_mapped` is `true` once these are the real positions. They are read every time the fixture is set, so an edit takes effect without restarting anything. If `order` is not exactly the fixture's bulbs, the order of `bulbs` stands in, as one ring, and `fixture status` says the layout is not mapped. Bulbs not named in `rings` are called "position 5" and so on, by their place in `order`. The older form, `centre` and `ring` (one bulb in the middle, the rest round it), still works.

### Brightness

Brightness is the average dimming of the twelve bulbs.

- **A whole number is every bulb at that dimming.** Fixture 1% is all twelve at 1, fixture 37% is all twelve at 37, and 100% is all twelve at 100. Apple Home only sends whole numbers, so from Home every bulb is always lit and they are all alike.
- **A fraction puts some bulbs one step higher.** Brightness moves in steps of one twelfth of a percent: one bulb, one step. Fixture 12.5% is 150 twelfths, so six bulbs are at 12 and six at 13.
- **Below 1% every bulb stays lit, on its raw white LEDs.** See the next section. Only below about 0.024% are bulbs switched off.

The command, `follow.json` and the sun follower take fractions. They are printed to one decimal place from 1% up ("37.6%") and to three below ("0.4%", "0.03%"), with the raw level when that is in use and the number of bulbs lit when that is not all of them: `white 2700K at 0.4% (raw warm 16.8)`.

### Which bulbs are singled out

The bulbs singled out, to take the extra step or to be the only ones lit, are chosen so the light stays even round the fixture. Of every way to pick that many bulbs, the one taken is:

1. the most even: the directions of the bulbs from the middle add up to the least, so no side is heavier;
2. the most spread: the closest two of the smaller group (the bulbs singled out, or the rest) are as far apart as they can be;
3. the fewest bulbs changed from the pattern for one bulb fewer, so a slow ramp does not shuffle the fixture;
4. and then simply the first, so it is always the same.

For the twelve, numbered 1 to 12 clockwise in `order` (1 is upper 1, 2 is lower 2, 3 is upper 2, ... 11 is upper 6, 12 is lower 1):

| Singled out | Which | Shape |
|---|---|---|
| 1 | 1 (upper 1) | one bulb |
| 2 | 1, 7 (upper 1, upper 4) | an opposite pair |
| 3 | 1, 5, 9 (upper 1, 3, 5) | a triangle |
| 4 | 1, 4, 7, 10 (upper 1, lower 3, upper 4, lower 6) | a square |
| 5 | 1, 2, 6, 7, 10 (upper 1, lower 2, lower 4, upper 4, lower 6) | a pair and a triangle |
| 6 | 2, 4, 6, 8, 10, 12 | every other one: the whole lower ring |
| 7 | all but 3, 6, 7, 11, 12 (upper 2, lower 4, upper 4, upper 6, lower 1) | all but a pair and a triangle |
| 8 | all but 3, 6, 9, 12 (upper 2, lower 4, upper 5, lower 1) | all but a square |
| 9 | all but 4, 8, 12 (lower 3, lower 5, lower 1) | all but a triangle |
| 10 | all but 6, 12 (lower 4, lower 1) | all but an opposite pair |
| 11 | all but 12 (lower 1) | all but one |

Every pattern except 1 and 11 is exactly balanced. The table is worked out from `order`, so it follows the layout if that changes; any number of bulbs works the same way. For the older form with a centre, the same rules give the old table: the centre, an opposite pair, a triangle, the centre and a triangle, the centre and two opposite pairs, the ring.

### Below 1%: the raw white LEDs

White at dimming 1 is as low as the bulbs' ordinary control goes, and they cut fractions off (1.5 is taken as 1). But the cold and warm white LEDs can also be set directly, as `c` and `w` from 0 to 255 at a dimming of 10, and that goes much lower. This is called raw here.

One raw setting has been matched by eye against ordinary white, on one bulb beside the others: **warm 42 at dimming 10 looks the same as white 2700K at dimming 1**, in brightness and in colour. 43 looked a touch brighter; 32 and 25 dimmer. That is the one line in `raw_calibration`.

So below 1%, white brightness B is every bulb at warm level 42 x B, at dimming 10:

- It moves in twelfths of a level, the same way as above 1%: every bulb at the whole level, and the left-over twelfths go one each to the singled-out bulbs. Fixture 0.4% is level 16.8: ten bulbs at 17 and two at 16.
- Below every bulb at level 1 (about 0.024%) there is nothing lower to send, so fewer bulbs are lit, each at level 1, by the same table. The least is one bulb at level 1, about 0.002%.
- At 1% and above nothing is raw: it is ordinary white, as described above.

**Only 2700K is matched.** At other colour temperatures below 1% the same total is shared between the cold and warm LEDs, evenly in mireds from 2700K (all warm) to 6500K (all cold), and warmer than 2700K is all warm. That is a guess at both the colour and the brightness. The sun follower's night colour is 2700K, so the matched case is the one it uses. `fixture status` says when the present setting is a guess.

Not yet known: whether a bulb shows a blip when it changes between ordinary white (at 1%) and raw (just below). `fixture sweep 1.2 0.8 --seconds 60` crosses it slowly for watching.

Without a 1% line in `raw_calibration`, or on bulbs whose lowest dimming is above 1, there is no raw white: below the lowest dimming fewer bulbs are lit, as for a colour.

### Raw white at every brightness

`"white_mode": "raw"` in `fixture.json` drives the white LEDs directly at every brightness, so a bulb never changes between the two ways. The default, `"hybrid"`, is raw below 1% only. It takes effect at the next change; restart the Apple Home service to be sure it has it (`launchctl kickstart -k gui/$(id -u)/local.fixture-homekit`).

**Do not switch to it yet.** It needs more measured points. `raw_calibration` holds them, one line each:

```
{"kelvin": 2700, "percent": 50, "c": 0, "w": 200, "dimming": 60}
```

That would say: cold 0 and warm 200 at dimming 60 looks the same as ordinary white 2700K at 50%. (The numbers are made up.) How the points are used:

- The light of a raw setting is taken as level x dimming, where the level is `c` + `w`.
- Between two points of the same colour temperature, the light and the dimming move evenly from one to the other.
- Outside the points it is a guess, in proportion to the nearest one. With only the 1% point, warm 255 at dimming 100 comes out as 61%, and everything above that is the same. That is very likely wrong, which is why more points are needed.
- A point also fixes the cold and warm mix at its colour temperature. Other colour temperatures use the points of the nearest one, with the mireds guess for the mix.
- A line that is not a whole, sensible point is ignored.

To measure a point, set the fixture to an ordinary white, then set one bulb by hand and change it until it matches its neighbours:

```
fixture white 2700 50
fixture compare --bulb "upper 1" --raw-w 200 --raw-dimming 60
fixture compare --bulb "upper 1" --raw-w 210 --raw-dimming 60
fixture compare --restore
```

`--bulb` takes a place from `rings` such as `"upper 3"`, a number round the fixture in `order` (1 to 12), or a MAC address. `--raw-c` is the cold LED. Left out, the levels are warm 1, cold 0, dimming 10, so a bulb is never made bright by accident. The other bulbs are not touched.

A comparison is not a change made by hand: the record is given the bulb's new look, so it does not turn Sun Tracking off, and the sun follower leaves the fixture alone meanwhile, for up to 30 minutes. `--restore` puts every bulb back as it was and hands the fixture back to whoever had set it. A comparison left longer than 30 minutes, or set over from Apple Home, is forgotten and the follower takes the fixture back.

### Colours

A colour cannot be dimmed below 10 on these bulbs. From 10% up a colour works exactly as white does. Below 10% only some bulbs are lit, each at 10, as many as come nearest to the light asked for (fixture 5% is four bulbs at 10), chosen by the same table. There is no raw mode for colours.

A fixture of some other number of bulbs works the same way, but has no hexagon: the bulbs singled out are simply the first ones in the list.

Bulbs that are kept off are still given the colour and the lowest level, so if the wall switch brings every bulb back on, they all look right. The fixture then switches the extra ones off again.

### Changes made elsewhere

The bulbs can be asked what they are doing, and they are: by `fixture status`, by the Apple Home service every 5 seconds, and by the sun follower every minute. `state.json` in the settings folder records what the fixture was last set to and by whom. When the bulbs differ from it, something else set them (the WiZ app, a bulb's own tile in Apple Home), and the record is changed to match the bulbs. Nothing is sent to undo it. The record keeps what each bulb should show, so bulbs that are deliberately a step apart are not mistaken for a change. The fixture's brightness is then the average of the bulbs as found.

Switching the whole fixture off and on is not a new setting, wherever it is done. Nor are these, which are put right without counting as anyone's change (so they never turn Sun Tracking off):

- **Power back at the wall, or after a power cut.** A bulb whose power comes back lights at its own power-on look, which on these bulbs is about 4200K at full brightness (`power_on_look` in `fixture.json`: temp 4207, dimming 100, as seen on 2026-11-18). A bulb found like that is sent what it should show. If the fixture was off, it is now on: it was switched on at the wall. Either way, by day the fixture then starts afresh at the sun's setting, within a few seconds.
- **A bulb new to the fixture**, just added to `bulbs`, is brought to what the others show. The list is read again every time, so the Apple Home service picks up a new bulb without a restart.
- **A bulb that missed the last setting, or did not answer at the last look**, is sent what it should show, as long as every other bulb is as it should be. (If they differ too, it is a change made elsewhere after all.)

### Apple Home

The accessory is its own program with its own port (51827), pairing keys and setup code. It is not part of the Neewer accessory and does not touch it. It runs under its own launcher app (`FixtureHomeKit.app`) so macOS has one named program to attach the local-network permission to. The sun follower runs through the same app, so one permission covers both.

**The first time, macOS asks whether Fixture HomeKit may find devices on your local network. Choose Allow.** Until then nothing can reach the bulbs or Apple Home, and both logs say so. The service restarts itself once it is allowed. If the question was missed, it is under System Settings > Privacy & Security > Local Network.

To pair, open the Home app, choose Add Accessory, and scan the QR code or enter the setup code:

```
"$HOME/Library/Application Support/NeewerBridge/venv/bin/python" \
  "$HOME/Library/Application Support/LightFixture/homekit/fixture_homekit.py" --pairing
```

Add `--qr code.svg` in place of `--pairing` to write the QR code to a file.

- Home shows one light: on/off, brightness, colour and colour temperature (2200-6500K).
- **Every write from Apple Home is logged**, one line each, with the address it came from, in `~/Library/Logs/fixture-homekit.log`: `Home wrote (from 192.168.1.149): The Sun Lightbulb Brightness=40`. The Neewer and Desk services do the same in their logs, so a change nobody remembers making can be traced to a phone, the home hub or an automation.
- **Dragging a slider.** While Home is writing one change after another, the newest is sent to every bulb ten times a second without waiting for the bulbs to answer. When nothing new has come for a third of a second, the last one is sent again the careful way, waiting for every bulb, and only then is the record written. `stream_rate` in `fixture.json` is the ten; 0 goes back to waiting for the bulbs after every send. Measured on the first seven bulbs with a two-second drag: each bulb showed 12 to 17 steps where it had shown 3 or 4, and all were at the last value 0.3 to 0.75 seconds after the drag ended, where it had taken 0.5 to 2.9. Twenty a second was no better.
- Home's brightness is a whole number. A fraction set elsewhere is shown rounded, and anything lit shows as at least 1%.
- A change made any other way shows up in Home within about 5 seconds.
- If no bulb answers (the wall switch is off), Home shows "No Response". One bulb answering is enough.
- The pairing keys are in `homekit-data/` in the settings folder, readable only by you. Deleting that folder unpairs the fixture; remove it from the Home app first.
- `name` and `port` are in `fixture.json`. Restart after changing them: `launchctl kickstart -k gui/$(id -u)/local.fixture-homekit`.

### Following the sun

The fixture follows the sun the same way the Neewer light does, from the same sundial file, and it is on from the start. Once a minute it sets brightness and colour temperature: full and cool white by day, dimming and warming through sunset, then low and warm all night.

```
fixture follow            # show what it is doing and why
fixture follow off        # pause it; the fixture stays as it is
fixture follow on         # start again (Sun Tracking is its own switch: fixture tracking on)
```

The rules are the Neewer light's:

- **It switches the fixture off and on only at bedtime and in the morning**, by the same rule as the Neewer light (see Bedtime above), with the same settings in its own `follow.json`. Otherwise it never does, and while the fixture is off it does nothing.
- **A setting made elsewhere wins.** A change from Apple Home, the `fixture` command, the WiZ app or a single bulb's tile turns sun tracking off, and the fixture is left alone until it is turned on again, with `fixture tracking on` or the Sun Tracking switch.
- **Switched on by day, it starts afresh**, as the Neewer light does (see A fresh start above), with `"fresh_start"` in its own `follow.json`. From Apple Home's Sun tile it is immediate: no bulb is sent the old setting first. From anywhere else (`fixture on`, the WiZ app, a bulb's own tile, the wall switch) the old setting shows until the follower's next look, up to a minute. Bulbs that did not answer, as when the wall switch is off, count as having been off.
- **It only sends when what a bulb should show changes**, and writes one line to the log each time. It does not round brightness to a whole number, so through dusk and dawn the fixture moves one bulb at a time, in twelfths of a percent.
- If no bulb answers, or sundial has stopped, it tries again each minute and writes one line, not one a minute.

The fixture's fade at bedtime goes down through the raw levels below 1%, to a single bulb at its lowest, and then every bulb is switched off. The bulbs are given the setting from before the fade as they go off, so switching the fixture on by hand, from anywhere, brings that back. In the morning every bulb comes back on.

`follow.json` in the fixture's settings folder has the same settings as the Neewer light's: `enabled`, `day_brightness` (100), `night_brightness` (1), `night_kelvin` (2700) and `hold_minutes` (120). Colour is kept within the bulbs' 2200-6500K. The night level of 1 is every bulb at dimming 1. It may be lower: 0.4 is every bulb on its raw warm LED at about level 17, and the ramp down to it at dusk is then smooth all the way.

`python3 tests/test_fixture.py` checks all of this against pretend bulbs (seven in the older form, and twelve as the fixture is now), without touching real ones.

## The desk (monitors and keyboard)

`desk/` puts two more things in Apple Home: the external monitors as one light, and the Wooting keyboard's lighting as another. They are one Apple Home bridge, "Desk", so there is one code to pair and two tiles. `./install.sh desk` installs it; the fixture has to be installed first.

Neither is set by this program itself. Each already has one program that sets it, and that stays so:

- **The monitors are sundial's.** `~/bin/sundial` runs every minute and makes the displays follow the sun (brightness through `m1ddc`, colour through Night Shift). The desk service only asks sundial.
- **The keyboard's LEDs are wooting-workspaces'.** That helper (in `~/.config/aerospace`) shows AeroSpace's workspaces on the keys. The desk service only writes a small file the helper reads.

The service runs under the fixture's launcher app, through `fixture host`, so it comes under the local-network permission that app already has. macOS asks nothing new.

```
"$HOME/Library/Application Support/NeewerBridge/venv/bin/python" \
  "$HOME/Library/Application Support/Desk/homekit/desk_homekit.py" --pairing   # the setup code
```

`--qr code.svg` writes the QR code, and `--status` says what the monitors and the keyboard are doing and why.

### Monitors

- **Brightness** 0-100 is the brightness of every external display.
- **Colour temperature** is 2700-6500K: what Night Shift can do. 2700K is the warmest macOS allows and 6000K the coolest tint; anything cooler means no tint, shown as 6500K. There is no colour wheel.
- **Off is brightness 0**, the backlight at its lowest, not asleep. The brightness held before is remembered, and "on" brings it back (or hands brightness back to the sun if none was held). `"off_means": "sleep"` in `desk.json` makes off sleep the displays and on wake them; that has not been tried on the real displays.
- **A change from Home turns sun tracking off** and is held until tracking is turned on again. (In the older way without the switch it is held for `hold_minutes`, two hours.) Brightness and colour are held separately when set, but while tracking is off both are held as they are.
- **Home is shown what sundial last applied**, so it moves through the day with the sun, and goes back to the sun's values when a hold runs out.
- With no external display connected, Home shows "No Response".

sundial has a mode for this, added for it: `sundial manual [MIN] [brightness=N|sun] [kelvin=K|sun]`. `sundial resume` ends it, as it ends pause and movie mode, and `sundial status` shows it. The three modes replace each other: a change from Home ends a pause or a movie mode, and `sundial pause` or `sundial movie` ends a hold from Home. During a pause Home shows the brightness sundial last set and 6500K.

**The lights do not follow a held monitor setting.** sundial's state file has the sun's own values under `target` and what the displays were sent under `applied`. A hold changes only `applied`. The two light followers and the keyboard read `target`.

`~/bin` is not under version control, so the sundial that goes with this is kept in `monitors/sundial` for reference. `install.sh` does not install it. sundial also takes a lock now while it changes anything, so the minute timer and a command cannot overwrite each other.

### Keyboard

Left alone, the keyboard follows the sun as the lights do: 25% and warm at night, rising with sundial's daylight to 100% and no tint by day. Home has On, Brightness and Colour temperature (2700-6500K, which sets how warm the tint is). A brightness or colour from Home turns sun tracking off and is held until tracking is turned on again. Off is every key dark.

The desk service writes `~/.local/state/wooting-workspaces/control`, and the helper re-reads it every second:

```
on 1
brightness 0.250
tint 1.000 0.656 0.350
base ffffff
```

The helper scales every colour it shows by `brightness` and, per channel, by `tint`. A workspace key that is lit stays lit however dim.

One thing to know. The keys the helper does not colour normally show the Wootility profile, which the keyboard draws itself and which cannot be dimmed from outside. So while the keyboard is dimmed or tinted, those keys are overridden with `base_colour` (white, so a warm white at night) and the profile is not seen. Only at 100% with no tint are they handed back to the profile. `"keep_profile_at_full": false` keeps the base colour then too.

The keyboard is not switched off at bedtime. It cannot be asked what it is showing, so Home shows what the control file says.

The change to the helper is in `keyboard/wooting-workspaces.patch` for reference; the helper itself lives in its own repository. Without the control file the helper behaves exactly as before, and `./uninstall.sh` deletes the file.

### Settings

`desk.json` in `~/Library/Application Support/Desk/`; leave out anything to use these:

```
{
  "name": "Desk",
  "port": 51828,
  "monitors": {"name": "Monitors", "hold_minutes": 120, "off_means": "dim"},
  "keyboard": {"name": "Keyboard", "hold_minutes": 120, "day_brightness": 100, "night_brightness": 25,
               "base_colour": "ffffff", "keep_profile_at_full": true}
}
```

Restart after changing a name or the port: `launchctl kickstart -k gui/$(id -u)/local.desk-homekit`. The rest is read as it is used. `python3 tests/test_desk.py` checks all of this, and sundial's manual mode, without displays or a keyboard.

## Things to know

- **The light must be in Infinity mode.** If its display shows `CH` and a number in the top-left, it is in 2.4G mode and Bluetooth is off. Hold the button marked `2.4G` for two seconds to switch.
- **The Mac must be logged in and awake.** macOS only allows Bluetooth inside a logged-in session. For a Sunrise automation, that means awake at dawn too.
- **The bridge does not know the light's Bluetooth address**, because macOS hides it from apps, but every command needs it. It is saved in `light.json`. `neewer address --detect` reads it from the system Bluetooth list.

## Files on the Mac

| What | Where |
|---|---|
| Bridge app | `~/Applications/NeewerBridge.app` |
| Apple Home launcher app | `~/Applications/NeewerHomeKit.app` |
| Background agents | `~/Library/LaunchAgents/local.neewer-bridge.plist`, `local.neewer-homekit.plist`, `local.neewer-follow.plist` |
| Command | `~/.local/bin/neewer` |
| Settings, last state, Python environment, pairing keys | `~/Library/Application Support/NeewerBridge/` |
| Logs | `~/Library/Logs/neewer-bridge.log`, `neewer-homekit.log`, `neewer-follow.log` |
| Fixture launcher app | `~/Applications/FixtureHomeKit.app` |
| Fixture agents | `~/Library/LaunchAgents/local.fixture-homekit.plist`, `local.fixture-follow.plist` |
| Fixture command | `~/.local/bin/fixture` |
| Fixture settings, last state, bulb addresses, pairing keys | `~/Library/Application Support/LightFixture/` |
| Fixture logs | `~/Library/Logs/fixture-homekit.log`, `fixture-follow.log` |
| Desk agent | `~/Library/LaunchAgents/local.desk-homekit.plist` |
| Desk settings, pairing keys | `~/Library/Application Support/Desk/` |
| Desk log | `~/Library/Logs/desk-homekit.log` |
| Keyboard control file | `~/.local/state/wooting-workspaces/control` |

`bridge.json` in the settings folder takes `port`, `namePrefixes`, `name`, `peripheralID`, `probeSeconds` and `logWrites`. Restart the bridge after changing it:

```
launchctl kickstart -k gui/$(id -u)/local.neewer-bridge
```

## Install, update, remove

```
./install.sh          # builds only if the bridge source changed
./install.sh cli      # reinstall just the command
./install.sh homekit  # install or update the Apple Home service
./install.sh follow   # make the light follow the sun
./install.sh fixture  # the WiZ ceiling fixture: command, Apple Home service, sun follower
./install.sh desk     # the monitors and the keyboard in Apple Home
./uninstall.sh        # add --purge to remove settings, pairing keys and logs too
```

## Command format

Confirmed on a CB200C that advertises as `NW-20250017&00000000`, and the power and white frames again on an AS600B that advertises as `NW-20240064&00000000`:

```
78 <tag> <size> <6 address bytes> <subtag> <values...> <checksum>

power   tag 8D  subtag 81  values: 01 on, 02 off
white   tag 90  subtag 87  values: brightness, kelvin/100, tint+50, dimming curve (04 = linear),
                           and on the AS600B the brightness's tenths (0-9)
colour  tag 8F  subtag 86  values: hue low, hue high, saturation, brightness, 00
channels tag A9 subtag A8  values: brightness, red, green, blue, cold white, warm white (0-255 each), 00
```

`size` counts every byte after itself up to the checksum. The checksum is the low byte of the sum of all bytes before it. `neewer selftest` checks the builder against known-good frames.

On the AS600B, judged by eye on its display: the power and white frames work as on the CB200C, and its display steps colour temperature in 100K, the same as the kelvin/100 byte. The curve byte is honoured (04 linear, 01 exponential). The white frame needs a fifth value, the tenths of the brightness, with size 0C: `78 90 0C F0 6F 33 D3 09 34 87 28 1E 32 01 00 B6` made its display read exactly exp 40.0 at 3000K. Sent without it, the light takes the missing tenths as 255, so the brightness arrives 25.5 too high: exp 45 read 70.5. A frame that comes out above 100.0 is dropped, but its curve byte still applies. The CB200C keeps the four-value frame. The channels frame is ignored. A white frame at 2500K or 6600K is dropped. It answers the device-info query with `... 41 53 36 30 30 42` ("AS600" "B") and the temperature query too.

What was tried on the CB200C, judged by eye:

- The older, shorter frames (`78 81 ...`, `78 87 ...`) are ignored.
- The channels frame works and reaches below 1%. Brightness 1 with warm white at 20, 5 and 1 out of 255 were each dimmer than the last, and all still lit.
- Red alone at level 1 (`neewer channels 1 --red 1`) is also lit, as a very dim red.
- Brightness 0 is off, whatever else the frame says. A white frame with brightness 0 and an extra "decimal brightness" byte is dark, and so is a channels frame with brightness 0 and a decimal value of 5.
- The dimming-curve byte in the white frame is ignored: the exponential and linear curves looked the same at 1% and at 10%.

So on the CB200C the floor is brightness 1 with one channel at level 1, and nothing found goes lower without switching the light off.

The CB200C answers some address-tagged queries: device info (`78 9E 07 <address> 00`) and temperature and fan (`78 B3 06 <address>`). It does not answer the power-state query (`78 8E 08 <address> 84 00`) or the old read request `78 84 00`. Its device-info reply names the model CB300C, although it was sold as a CB200C; the two manuals differ only in wattage.

The channels frame and the queries come from the protocol notes in [neewer-sacn](https://github.com/amattas/neewer-sacn).

## Credits

The command format was worked out by others. This project leans on the protocol notes and source of [NeewerLite](https://github.com/keefo/NeewerLite) and [NeewerLite-Python](https://github.com/taburineagle/NeewerLite-Python), and only confirmed that the CB200C with product code 20250017 uses the address-tagged frames.

This is an independent project and is not affiliated with NEEWER.

## Licence

MIT. See [LICENSE](LICENSE).
