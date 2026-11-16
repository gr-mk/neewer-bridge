#!/bin/bash
# Builds and installs Neewer Bridge for the current user.
# Run it on the Mac that sits within Bluetooth range of the light.
#
#   ./install.sh           build the bridge if its source changed, install the
#                          `neewer` command, and (re)load the bridge agent
#   ./install.sh cli       reinstall only the `neewer` command
#   ./install.sh homekit   install or update the Apple Home service
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

case "${1:-}" in
"") install_bridge ;;
cli) install_cli ;;
homekit) install_homekit ;;
*)
	echo "usage: $0 [cli|homekit]" >&2
	exit 2
	;;
esac
