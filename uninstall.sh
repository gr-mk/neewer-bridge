#!/bin/bash
# Removes Neewer Bridge and the WiZ fixture for the current user.
#
#   ./uninstall.sh           stop the agents and remove the apps and the command
#   ./uninstall.sh --purge   also remove settings, the Python environment, the
#                            Apple Home pairing keys and the logs
#
# Remove the lights from the Home app first; otherwise they stay there as "No Response".

set -euo pipefail

DOMAIN="gui/$(id -u)"
CLI_DIR="${NEEWER_CLI_DIR:-$HOME/.local/bin}"

for label in local.fixture-follow local.fixture-homekit local.neewer-follow local.neewer-homekit local.neewer-bridge; do
	launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
	rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
rm -rf "$HOME/Applications/NeewerBridge.app" "$HOME/Applications/NeewerHomeKit.app" \
	"$HOME/Applications/FixtureHomeKit.app"
rm -f "$CLI_DIR/neewer" "$CLI_DIR/fixture"
echo "removed the agents, the apps and the neewer and fixture commands"

if [ "${1:-}" = "--purge" ]; then
	rm -rf "$HOME/Library/Application Support/NeewerBridge" "$HOME/Library/Application Support/LightFixture"
	rm -f "$HOME/Library/Logs/neewer-bridge.log" "$HOME/Library/Logs/neewer-homekit.log" \
		"$HOME/Library/Logs/neewer-follow.log" "$HOME/Library/Logs/fixture-homekit.log" \
		"$HOME/Library/Logs/fixture-follow.log"
	echo "removed settings, the Python environment, pairing keys and logs"
fi

echo "To also drop the Bluetooth permission entry: tccutil reset BluetoothAlways local.neewer-bridge"
