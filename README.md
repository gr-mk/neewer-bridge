# Neewer Bridge

Controls a Neewer CB200C over Bluetooth from a Mac, with no window and no vendor app.

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
neewer white 5600 10        # colour temperature in kelvin (2500-7500), brightness 0-100
neewer white 3200 --tint -10
neewer colour 240 100 10    # hue 0-360, saturation 0-100, brightness
neewer rgb ff8800 10
neewer brightness 1         # keeps the current colour; 1 is the lowest ordinary level
neewer faint 5              # dimmer than 1%: level 1 (dimmest) to 255
neewer faint 5 --white both # use the cold white LEDs as well as the warm ones
neewer channels 1 --warm 20 # drive the five LED channels directly, 0-255 each
neewer sunrise --minutes 30 # ramp up the way a real sunrise brightens
neewer follow               # show whether the light is following the sun
neewer status
```

Brightness is a whole number, and 1% is as low as the ordinary control goes. `neewer faint` goes below that. It uses the light's five-channel mode at brightness 1 with only the white LEDs lit, which is much dimmer than `white ... 1` and is still visibly lit at level 1.

`neewer status` shows what the command last sent, not what the light is doing: the light does not report its state, so changes made on its own dial are not seen.

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

The whole ramp is sent as channels frames, mixing the warm and cold white LEDs, and never uses the white frame. Switching between the two frames shows a wrong colour for a moment, and the channels frame takes fractions of a percent all the way up. The mix was matched by eye against the white frame at 2800K, at 1% and at 10%: channels brightness `b` with levels adding up to 176 is as bright as white at `b`%, 2800K is about 95% warm and 5% cold, and about 1 part in 60 of red takes off a green cast. It has not been compared at other colour temperatures or near full brightness.

`neewer sunrise --style glow` is the older, designed ramp: a red glow, then warm white, then white, each climbing by a constant ratio.

Setting the light any other way, from Apple Home or with another `neewer` command, stops the ramp. When it finishes, the light is left in the channels mode, which Apple Home shows as white.

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
neewer follow on         # start again, and take the light over now
```

The rules:

- **It switches the light off and on only at bedtime and in the morning** (below). Otherwise it never does, and while the light is off it does nothing.
- **A setting made elsewhere wins.** If you set the light from Apple Home or with another `neewer` command, it leaves the light alone for two hours from that change, then follows again. `neewer follow on` ends the wait early.
- **Switched on by day, it starts afresh** (below): it goes to the sun's setting, not back to how it was left.
- **It keeps out of a sunrise.** Nothing is sent while `neewer sunrise` runs. What the sunrise leaves is then held for two hours like any other setting.
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

- Brightness runs from `night_brightness` to `day_brightness` with sundial's "daylight" share. With sundial's defaults that is the night level until the sun is 6 degrees below the horizon and the day level once it is 20 degrees up.
- Colour is sundial's colour, kept within the light's 2500-7500K. While the sun goes from the horizon to 6 degrees below, it moves to `night_kelvin`.
- `night_brightness` cannot go below 1, because 0 would switch the light off.
- `hold_minutes` is how long a setting made elsewhere is left alone.

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
- Switching on by hand after the fade brings back the setting from before the fade, not its last step. From Apple Home and the `neewer` command that is certain. The light's own button may wake it at 1%: the old setting is sent to the light as it is switched off, but whether it keeps a setting sent in standby is not known.
- A `neewer sunrise` running at bedtime or at the morning time is left to itself.

`neewer follow` says what the rule will do next: `off at 23:00, on again at 06:30`, `off for the night; on at 06:30`, `on by hand since 01:10; left alone tonight`.

`"bedtime_rule": false` switches the whole rule off. `"morning_on": false` keeps the switching off and drops the switching on. `fade_minutes` and `morning_lead_minutes` are the five minutes and the half hour.

It uses the ordinary white frame. After a sunrise the light is in the channels mode, so there is one brief wrong colour when following takes over.

Each change goes to `~/Library/Logs/neewer-follow.log`. `python3 tests/test_follow.py` checks the rules without a light.

## Apple Home

`./install.sh homekit` installs the service. It runs [HAP-python](https://github.com/ikalchev/HAP-python) in its own Python environment, under a small launcher app (`NeewerHomeKit.app`) so macOS has one named program to attach the local-network permission to.

To pair, open the Home app, choose Add Accessory, and scan the QR code or enter the setup code. Home will warn that the accessory is uncertified; that is expected. Show the code with:

```
"$HOME/Library/Application Support/NeewerBridge/venv/bin/python" \
  "$HOME/Library/Application Support/NeewerBridge/homekit/neewer_homekit.py" --pairing
```

Add `--qr code.svg` in place of `--pairing` to write the QR code to a file.

- Home shows the light as on/off, brightness, colour and colour temperature (2500-7500K).
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
fixture compare --bulb centre --raw-w 42   # set one bulb by hand; --restore puts it back
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
  "centre": "a1b2c3d4e5f6",
  "ring": ["...", "...", "...", "...", "...", "..."],
  "ring_names": ["north", "northeast", "southeast", "south", "southwest", "northwest"],
  "layout_mapped": false,
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

The fixture is one bulb in the centre with six in a hexagon round it. `centre` in `fixture.json` is the centre bulb's MAC address, and `ring` is the other six in order round the hexagon (either direction, starting anywhere). They are read every time the fixture is set, so an edit takes effect without restarting anything. `ring_names` is optional: six labels for the ring places, shown by `fixture status` and used for nothing else.

Until someone has looked at the fixture and filled these in, the order of `bulbs` stands in: the first is taken for the centre and the next six for the ring. `layout_mapped` is `false` to say so, and `fixture status` says so too. Set it to `true` once `centre` and `ring` are the real positions. If `centre` and `ring` are not exactly the seven bulbs of the fixture, they are ignored and the list order is used.

### Brightness

Brightness is the average dimming of the seven bulbs.

- **A whole number is every bulb at that dimming.** Fixture 1% is all seven at 1, fixture 37% is all seven at 37, and 100% is all seven at 100. Apple Home only sends whole numbers, so from Home every bulb is always lit and they are all alike.
- **A fraction puts some bulbs one step higher.** Brightness moves in steps of one seventh of a percent: one bulb, one step. Fixture 12.5% is 88 sevenths, so three bulbs are at 12 and four at 13.
- **Below 1% every bulb stays lit, on its raw white LEDs.** See the next section. Only below about 0.024% are bulbs switched off.

The command, `follow.json` and the sun follower take fractions. They are printed to one decimal place from 1% up ("37.6%") and to three below ("0.4%", "0.03%"), with the raw level when that is in use and the number of bulbs lit when that is not all of them: `white 2700K at 0.4% (raw warm 16.9)`.

The bulbs that are singled out, to take the extra step or to be the only ones lit, are chosen so the fixture stays symmetric. With the ring places numbered 0 to 5:

| Bulbs singled out | Which |
|---|---|
| 1 | the centre |
| 2 | ring 0 and 3, an opposite pair |
| 3 | ring 0, 2 and 4, a triangle |
| 4 | the centre and ring 0, 2 and 4 |
| 5 | the centre and ring 0, 1, 3 and 4, two opposite pairs |
| 6 | the whole ring, centre left out |

### Below 1%: the raw white LEDs

White at dimming 1 is as low as the bulbs' ordinary control goes, and they cut fractions off (1.5 is taken as 1). But the cold and warm white LEDs can also be set directly, as `c` and `w` from 0 to 255 at a dimming of 10, and that goes much lower. This is called raw here.

One raw setting has been matched by eye against ordinary white, on one bulb beside the others: **warm 42 at dimming 10 looks the same as white 2700K at dimming 1**, in brightness and in colour. 43 looked a touch brighter; 32 and 25 dimmer. That is the one line in `raw_calibration`.

So below 1%, white brightness B is every bulb at warm level 42 x B, at dimming 10:

- It moves in sevenths of a level, the same way as above 1%: every bulb at the whole level, and the left-over sevenths go one each to the singled-out bulbs. Fixture 0.4% is level 16.9: six bulbs at 17 and one at 16.
- Below every bulb at level 1 (about 0.024%) there is nothing lower to send, so fewer bulbs are lit, each at level 1, by the same table. The least is one bulb at level 1, about 0.003%.
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
fixture compare --bulb centre --raw-w 200 --raw-dimming 60
fixture compare --bulb centre --raw-w 210 --raw-dimming 60
fixture compare --restore
```

`--bulb` takes `centre`, a name from `ring_names` such as `north`, `ring 0` to `ring 5`, or a MAC address. `--raw-c` is the cold LED. Left out, the levels are warm 1, cold 0, dimming 10, so a bulb is never made bright by accident. The other bulbs are not touched. While a bulb is set by hand the sun follower leaves the fixture alone; `--restore` puts every bulb back as it was and hands the fixture back to whoever had set it, so no two-hour wait is left behind.

### Colours

A colour cannot be dimmed below 10 on these bulbs. From 10% up a colour works exactly as white does. Below 10% only some bulbs are lit, each at 10, as many as come nearest to the light asked for (fixture 5% is four bulbs at 10), chosen by the same table. There is no raw mode for colours.

A fixture of some other number of bulbs works the same way, but has no hexagon: the bulbs singled out are simply the first ones in the list.

Bulbs that are kept off are still given the colour and the lowest level, so if the wall switch brings every bulb back on, they all look right. The fixture then switches the extra ones off again.

### Changes made elsewhere

The bulbs can be asked what they are doing, and they are: by `fixture status`, by the Apple Home service every 5 seconds, and by the sun follower every minute. `state.json` in the settings folder records what the fixture was last set to and by whom. When the bulbs differ from it, something else set them (the WiZ app, a bulb's own tile in Apple Home), and the record is changed to match the bulbs. Nothing is sent to undo it. The record keeps what each bulb should show, so bulbs that are deliberately a step apart are not mistaken for a change. The fixture's brightness is then the average of the bulbs as found.

Switching the whole fixture off and on is not a new setting, wherever it is done.

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
- **Dragging a slider.** While Home is writing one change after another, the newest is sent to every bulb ten times a second without waiting for the bulbs to answer. When nothing new has come for a third of a second, the last one is sent again the careful way, waiting for every bulb, and only then is the record written. `stream_rate` in `fixture.json` is the ten; 0 goes back to waiting for the bulbs after every send. Measured on the seven bulbs with a two-second drag: each bulb showed 12 to 17 steps where it had shown 3 or 4, and all were at the last value 0.3 to 0.75 seconds after the drag ended, where it had taken 0.5 to 2.9. Twenty a second was no better.
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
fixture follow on         # start again, and take the fixture over now
```

The rules are the Neewer light's:

- **It switches the fixture off and on only at bedtime and in the morning**, by the same rule as the Neewer light (see Bedtime above), with the same settings in its own `follow.json`. Otherwise it never does, and while the fixture is off it does nothing.
- **A setting made elsewhere wins.** After a change from Apple Home, the `fixture` command, the WiZ app or a single bulb's tile, it leaves the fixture alone for two hours, then follows again. `fixture follow on` ends the wait early.
- **Switched on by day, it starts afresh**, as the Neewer light does (see A fresh start above), with `"fresh_start"` in its own `follow.json`. From Apple Home's Sun tile it is immediate: no bulb is sent the old setting first. From anywhere else (`fixture on`, the WiZ app, a bulb's own tile, the wall switch) the old setting shows until the follower's next look, up to a minute. Bulbs that did not answer, as when the wall switch is off, count as having been off.
- **It only sends when what a bulb should show changes**, and writes one line to the log each time. It does not round brightness to a whole number, so through dusk and dawn the fixture moves one bulb at a time, in sevenths of a percent.
- If no bulb answers, or sundial has stopped, it tries again each minute and writes one line, not one a minute.

The fixture's fade at bedtime goes down through the raw levels below 1%, to a single bulb at its lowest, and then every bulb is switched off. The bulbs are given the setting from before the fade as they go off, so switching the fixture on by hand, from anywhere, brings that back. In the morning every bulb comes back on.

`follow.json` in the fixture's settings folder has the same settings as the Neewer light's: `enabled`, `day_brightness` (100), `night_brightness` (1), `night_kelvin` (2700) and `hold_minutes` (120). Colour is kept within the bulbs' 2200-6500K. The night level of 1 is every bulb at dimming 1. It may be lower: 0.4 is every bulb on its raw warm LED at about level 17, and the ramp down to it at dusk is then smooth all the way.

`python3 tests/test_fixture.py` checks all of this against seven pretend bulbs, without touching real ones.

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
- **A change from Home is held for two hours** (`hold_minutes`), then the monitors follow the sun again. Brightness and colour are held separately: change only the brightness and the colour keeps following the sun.
- **Home is shown what sundial last applied**, so it moves through the day with the sun, and goes back to the sun's values when a hold runs out.
- With no external display connected, Home shows "No Response".

sundial has a mode for this, added for it: `sundial manual [MIN] [brightness=N|sun] [kelvin=K|sun]`. `sundial resume` ends it, as it ends pause and movie mode, and `sundial status` shows it. The three modes replace each other: a change from Home ends a pause or a movie mode, and `sundial pause` or `sundial movie` ends a hold from Home. During a pause Home shows the brightness sundial last set and 6500K.

**The lights do not follow a held monitor setting.** sundial's state file has the sun's own values under `target` and what the displays were sent under `applied`. A hold changes only `applied`. The two light followers and the keyboard read `target`.

`~/bin` is not under version control, so the sundial that goes with this is kept in `monitors/sundial` for reference. `install.sh` does not install it. sundial also takes a lock now while it changes anything, so the minute timer and a command cannot overwrite each other.

### Keyboard

Left alone, the keyboard follows the sun as the lights do: 25% and warm at night, rising with sundial's daylight to 100% and no tint by day. Home has On, Brightness and Colour temperature (2700-6500K, which sets how warm the tint is). A change from Home is held for two hours. Off is every key dark.

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

Confirmed on a CB200C that advertises as `NW-20250017&00000000`:

```
78 <tag> <size> <6 address bytes> <subtag> <values...> <checksum>

power   tag 8D  subtag 81  values: 01 on, 02 off
white   tag 90  subtag 87  values: brightness, kelvin/100, tint+50, dimming curve (04 = linear)
colour  tag 8F  subtag 86  values: hue low, hue high, saturation, brightness, 00
channels tag A9 subtag A8  values: brightness, red, green, blue, cold white, warm white (0-255 each), 00
```

`size` counts every byte after itself up to the checksum. The checksum is the low byte of the sum of all bytes before it. `neewer selftest` checks the builder against known-good frames.

What was tried on this light, judged by eye:

- The older, shorter frames (`78 81 ...`, `78 87 ...`) are ignored.
- The channels frame works and reaches below 1%. Brightness 1 with warm white at 20, 5 and 1 out of 255 were each dimmer than the last, and all still lit.
- Red alone at level 1 (`neewer channels 1 --red 1`) is also lit, as a very dim red.
- Brightness 0 is off, whatever else the frame says. A white frame with brightness 0 and an extra "decimal brightness" byte is dark, and so is a channels frame with brightness 0 and a decimal value of 5.
- The dimming-curve byte in the white frame is ignored: the exponential and linear curves looked the same at 1% and at 10%.

So the floor is brightness 1 with one channel at level 1, and nothing found goes lower without switching the light off.

The light answers some address-tagged queries: device info (`78 9E 07 <address> 00`) and temperature and fan (`78 B3 06 <address>`). It does not answer the power-state query (`78 8E 08 <address> 84 00`) or the old read request `78 84 00`. Its device-info reply names the model CB300C, although it was sold as a CB200C; the two manuals differ only in wattage.

The channels frame and the queries come from the protocol notes in [neewer-sacn](https://github.com/amattas/neewer-sacn).

## Credits

The command format was worked out by others. This project leans on the protocol notes and source of [NeewerLite](https://github.com/keefo/NeewerLite) and [NeewerLite-Python](https://github.com/taburineagle/NeewerLite-Python), and only confirmed that the CB200C with product code 20250017 uses the address-tagged frames.

This is an independent project and is not affiliated with NEEWER.

## Licence

MIT. See [LICENSE](LICENSE).
