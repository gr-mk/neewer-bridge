#!/bin/bash
# Builds and installs Neewer Bridge for the current user.
# Run it on the Mac that sits within Bluetooth range of the light.
#
#   ./install.sh           build the bridge if its source changed, install the
#                          `neewer` command, and (re)load the bridge agent
#   ./install.sh cli       reinstall only the `neewer` command
#   ./install.sh homekit   install or update the Apple Home service
#   ./install.sh follow    make the light follow the sun, once a minute
#   ./install.sh fixture   install the `fixture` command, its Apple Home
#                          service and its sun follower (WiZ bulbs as one light)
#
# Rebuilding the bridge changes its code signature, and macOS then asks for
# Bluetooth permission again. That is why the build is skipped when the bridge
# source is unchanged, and why the command format lives in the `neewer` script.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/NeewerBridge"
CLI_DIR="${NEEWER_CLI_DIR:-$HOME/.local/bin}"
DOMAIN="gui/$(id -u)"
HAP_VERSION="5.2.0"

install_cli() {
	mkdir -p "$CLI_DIR"
	install -m 755 "$HERE/cli/neewer" "$CLI_DIR/neewer"
	echo "installed $CLI_DIR/neewer"
}

# build_app <app path> <executable name> <bundle id> <source dir> <swift file>
# Compiles and signs only when the sources changed since the last build.
build_app() {
	local app="$1" exe="$2" id="$3" dir="$4" swift="$5"
	local binary="$app/Contents/MacOS/$exe" stamp="$app/Contents/Resources/source.sha256" hash
	hash="$(cat "$dir/$swift" "$dir/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
	if [ -x "$binary" ] && [ "$(cat "$stamp" 2>/dev/null || true)" = "$hash" ]; then
		echo "$(basename "$app") is up to date; not rebuilding"
		return
	fi
	echo "building $(basename "$app")..."
	mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
	cp "$dir/Info.plist" "$app/Contents/Info.plist"
	# The Info.plist is also embedded in the binary so macOS finds the usage
	# text however it resolves the process.
	swiftc -O -swift-version 5 -o "$binary" "$dir/$swift" \
		-Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$dir/Info.plist"
	echo "$hash" >"$stamp"
	codesign --force --sign - --identifier "$id" "$app"
}

# load_agent <label> <program> <log file>
load_agent() {
	local label="$1" program="$2" log="$3"
	local agent="$HOME/Library/LaunchAgents/$label.plist"
	mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
	cat >"$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>$program</string>
	</array>
	<key>AssociatedBundleIdentifiers</key>
	<string>$label</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<true/>
	<key>ThrottleInterval</key>
	<integer>15</integer>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>StandardOutPath</key>
	<string>$log</string>
	<key>StandardErrorPath</key>
	<string>$log</string>
</dict>
</plist>
EOF
	launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
	local attempt
	for attempt in 1 2 3 4 5; do
		if launchctl bootstrap "$DOMAIN" "$agent" 2>/dev/null; then
			echo "agent $label loaded; log at $log"
			return
		fi
		sleep 1 # the old instance may still be shutting down
	done
	echo "could not load the agent; try: launchctl bootstrap $DOMAIN \"$agent\"" >&2
	exit 1
}

install_bridge() {
	local app="$HOME/Applications/NeewerBridge.app"
	mkdir -p "$SUPPORT"
	if [ ! -f "$SUPPORT/bridge.json" ]; then
		printf '{\n  "port": 18490,\n  "namePrefixes": ["NW-", "NEEWER"],\n  "logWrites": false\n}\n' >"$SUPPORT/bridge.json"
		echo "wrote default $SUPPORT/bridge.json"
	fi
	build_app "$app" neewer-bridge local.neewer-bridge "$HERE/bridge" main.swift
	install_cli
	load_agent local.neewer-bridge "$app/Contents/MacOS/neewer-bridge" "$HOME/Library/Logs/neewer-bridge.log"
	echo "If macOS asks whether Neewer Bridge may use Bluetooth, choose Allow."
}

install_homekit() {
	local app="$HOME/Applications/NeewerHomeKit.app" venv="$SUPPORT/venv" uv
	uv="$(command -v uv || true)"
	[ -n "$uv" ] || uv=/opt/homebrew/bin/uv
	if [ ! -x "$uv" ]; then
		echo "uv is needed to create the Python environment (brew install uv)" >&2
		exit 1
	fi
	mkdir -p "$SUPPORT/homekit"
	[ -x "$venv/bin/python" ] || "$uv" venv --python 3.13 "$venv"
	"$uv" pip install --quiet --python "$venv/bin/python" "HAP-python[QRCode]==$HAP_VERSION"
	install -m 644 "$HERE/homekit/neewer_homekit.py" "$SUPPORT/homekit/neewer_homekit.py"
	# The service reuses the command's frame builder, so both always agree.
	install -m 644 "$HERE/cli/neewer" "$SUPPORT/homekit/neewer_cli.py"
	build_app "$app" neewer-homekit local.neewer-homekit "$HERE/homekit" launcher.swift
	load_agent local.neewer-homekit "$app/Contents/MacOS/neewer-homekit" "$HOME/Library/Logs/neewer-homekit.log"
	echo "If macOS asks whether Neewer HomeKit may find devices on your local network, choose Allow."
	echo "Setup code: \"$venv/bin/python\" \"$SUPPORT/homekit/neewer_homekit.py\" --pairing"
}

# Runs `neewer follow apply` once a minute. Unlike the two services this is a
# timer, not a program that stays running, so it has its own agent file.
install_follow() {
	local label=local.neewer-follow log="$HOME/Library/Logs/neewer-follow.log"
	local agent="$HOME/Library/LaunchAgents/$label.plist"
	if [ ! -f "$HOME/.local/state/sundial/state.json" ]; then
		echo "note: sundial has written no state file yet; the light is left alone until it does" >&2
	fi
	install_cli
	mkdir -p "$SUPPORT" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
	if [ ! -f "$SUPPORT/follow.json" ]; then
		printf '{\n  "enabled": true,\n  "day_brightness": 100,\n  "night_brightness": 5,\n  "night_kelvin": 2700,\n  "hold_minutes": 120\n}\n' >"$SUPPORT/follow.json"
		echo "wrote default $SUPPORT/follow.json"
	fi
	cat >"$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>$CLI_DIR/neewer</string>
		<string>follow</string>
		<string>apply</string>
	</array>
	<key>StartInterval</key>
	<integer>60</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>StandardOutPath</key>
	<string>$log</string>
	<key>StandardErrorPath</key>
	<string>$log</string>
</dict>
</plist>
EOF
	launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
	local attempt
	for attempt in 1 2 3 4 5; do
		if launchctl bootstrap "$DOMAIN" "$agent" 2>/dev/null; then
			echo "agent $label loaded; log at $log"
			echo "Pause it with: neewer follow off"
			return
		fi
		sleep 1
	done
	echo "could not load the agent; try: launchctl bootstrap $DOMAIN \"$agent\"" >&2
	exit 1
}

# The fixture: several WiZ bulbs driven as one light. It shares nothing with
# the Neewer light but the Python environment.
install_fixture() {
	local support="$HOME/Library/Application Support/LightFixture"
	local app="$HOME/Applications/FixtureHomeKit.app" venv="$SUPPORT/venv" uv
	local label=local.fixture-follow log="$HOME/Library/Logs/fixture-follow.log"
	local agent="$HOME/Library/LaunchAgents/local.fixture-follow.plist"
	if [ ! -x "$venv/bin/python" ]; then
		uv="$(command -v uv || true)"
		[ -n "$uv" ] || uv=/opt/homebrew/bin/uv
		if [ ! -x "$uv" ]; then
			echo "uv is needed to create the Python environment (brew install uv)" >&2
			exit 1
		fi
		"$uv" venv --python 3.13 "$venv"
		"$uv" pip install --quiet --python "$venv/bin/python" "HAP-python[QRCode]==$HAP_VERSION"
	fi
	mkdir -p "$CLI_DIR" "$support/homekit" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
	install -m 755 "$HERE/fixture/fixture" "$CLI_DIR/fixture"
	echo "installed $CLI_DIR/fixture"
	install -m 644 "$HERE/fixture/fixture_homekit.py" "$support/homekit/fixture_homekit.py"
	# The service and the follower run the command's own code, so all three agree.
	install -m 644 "$HERE/fixture/fixture" "$support/homekit/fixture_cli.py"
	if [ ! -f "$support/fixture.json" ]; then
		echo "looking for WiZ bulbs; every one found becomes part of the fixture"
		"$CLI_DIR/fixture" discover --save ||
			echo "none found; switch the fixture on and run: fixture discover --save" >&2
	fi
	if [ ! -f "$support/follow.json" ]; then
		printf '{\n  "enabled": true,\n  "day_brightness": 100,\n  "night_brightness": 5,\n  "night_kelvin": 2700,\n  "hold_minutes": 120\n}\n' >"$support/follow.json"
		echo "wrote default $support/follow.json"
	fi
	if [ ! -f "$HOME/.local/state/sundial/state.json" ]; then
		echo "note: sundial has written no state file yet; the fixture is left alone until it does" >&2
	fi
	build_app "$app" fixture-homekit local.fixture-homekit "$HERE/fixture" launcher.swift
	load_agent local.fixture-homekit "$app/Contents/MacOS/fixture-homekit" "$HOME/Library/Logs/fixture-homekit.log"
	# The follower is a timer. It runs through the launcher app too, so its
	# packets to the bulbs fall under the same local-network permission.
	cat >"$agent" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>ProgramArguments</key>
	<array>
		<string>$app/Contents/MacOS/fixture-homekit</string>
		<string>follow</string>
		<string>apply</string>
	</array>
	<key>AssociatedBundleIdentifiers</key>
	<string>local.fixture-homekit</string>
	<key>StartInterval</key>
	<integer>60</integer>
	<key>RunAtLoad</key>
	<true/>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>StandardOutPath</key>
	<string>$log</string>
	<key>StandardErrorPath</key>
	<string>$log</string>
</dict>
</plist>
EOF
	launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
	local attempt loaded=no
	for attempt in 1 2 3 4 5; do
		if launchctl bootstrap "$DOMAIN" "$agent" 2>/dev/null; then
			loaded=yes
			break
		fi
		sleep 1
	done
	if [ "$loaded" = no ]; then
		echo "could not load the agent; try: launchctl bootstrap $DOMAIN \"$agent\"" >&2
		exit 1
	fi
	echo "agent $label loaded; log at $log"
	echo "If macOS asks whether Fixture HomeKit may find devices on your local network, choose Allow."
	echo "Setup code: \"$venv/bin/python\" \"$support/homekit/fixture_homekit.py\" --pairing"
	echo "Pause the sun follower with: fixture follow off"
}

case "${1:-}" in
"") install_bridge ;;
cli) install_cli ;;
homekit) install_homekit ;;
follow) install_follow ;;
fixture) install_fixture ;;
*)
	echo "usage: $0 [cli|homekit|follow|fixture]" >&2
	exit 2
	;;
esac
