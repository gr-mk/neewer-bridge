# Neewer Bridge

Controls a Neewer CB200C over Bluetooth from a Mac, with no window and no vendor app.

It has three parts:

- **The bridge** (`bridge/main.swift`) is a background program that holds the macOS Bluetooth permission. It finds the light, stays connected, and writes whatever bytes it is given. It listens on `127.0.0.1:18490` only.
- **The `neewer` command** (`cli/neewer`) builds the light's command frames and hands them to the bridge. The whole command format lives in this one file.
- **The Apple Home service** (`homekit/`) is optional. It presents the light to Apple Home as one colour bulb and sends its commands through the bridge.

The split is deliberate. Rebuilding the bridge changes its code signature, and macOS then asks for Bluetooth permission again. Changing the `neewer` script never does.

## Use

```
neewer on
neewer off
neewer white 5600 10        # colour temperature in kelvin (2500-7500), brightness 0-100
neewer white 3200 --tint -10
neewer colour 240 100 10    # hue 0-360, saturation 0-100, brightness
neewer rgb ff8800 10
neewer brightness 1         # keeps the current colour; 1 is the lowest lit level
neewer status
```

Brightness is a whole number. The light has no steps below 1%.

`neewer status` shows what the command last sent, not what the light is doing: the light does not report its state, so changes made on its own dial are not seen.

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
- `homekit.json` in the settings folder takes `name` and `port` (default 51826).

## Things to know

- **The light must be in Infinity mode.** If its display shows `CH` and a number in the top-left, it is in 2.4G mode and Bluetooth is off. Hold the button marked `2.4G` for two seconds to switch.
- **The Mac must be logged in and awake.** macOS only allows Bluetooth inside a logged-in session.
- **The bridge does not know the light's Bluetooth address**, because macOS hides it from apps, but every command needs it. It is saved in `light.json`. `neewer address --detect` reads it from the system Bluetooth list.

## Files on the Mac

| What | Where |
|---|---|
| Bridge app | `~/Applications/NeewerBridge.app` |
| Apple Home launcher app | `~/Applications/NeewerHomeKit.app` |
| Background agents | `~/Library/LaunchAgents/local.neewer-bridge.plist`, `local.neewer-homekit.plist` |
| Command | `~/.local/bin/neewer` |
| Settings, last state, Python environment, pairing keys | `~/Library/Application Support/NeewerBridge/` |
| Logs | `~/Library/Logs/neewer-bridge.log`, `neewer-homekit.log` |

`bridge.json` in the settings folder takes `port`, `namePrefixes`, `name`, `peripheralID`, `probeSeconds` and `logWrites`. Restart the bridge after changing it:

```
launchctl kickstart -k gui/$(id -u)/local.neewer-bridge
```

## Install, update, remove

```
./install.sh          # builds only if the bridge source changed
./install.sh cli      # reinstall just the command
./install.sh homekit  # install or update the Apple Home service
./uninstall.sh        # add --purge to remove settings, pairing keys and logs too
```

## Command format

Confirmed on a CB200C that advertises as `NW-20250017&00000000`:

```
78 <tag> <size> <6 address bytes> <subtag> <values...> <checksum>

power   tag 8D  subtag 81  values: 01 on, 02 off
white   tag 90  subtag 87  values: brightness, kelvin/100, tint+50, dimming curve (04 = linear)
colour  tag 8F  subtag 86  values: hue low, hue high, saturation, brightness, 00
```

`size` counts every byte after itself up to the checksum. The checksum is the low byte of the sum of all bytes before it. `neewer selftest` checks the builder against frames captured from real lights.

The older, shorter frames (`78 81 ...`, `78 87 ...`) are ignored by this light. The light sends nothing back, including to the read request `78 84 00`.

## Credits

The command format was worked out by others. This project leans on the protocol notes and source of [NeewerLite](https://github.com/keefo/NeewerLite) and [NeewerLite-Python](https://github.com/taburineagle/NeewerLite-Python), and only confirmed that the CB200C with product code 20250017 uses the address-tagged frames.

This is an independent project and is not affiliated with NEEWER.

## Licence

MIT. See [LICENSE](LICENSE).
