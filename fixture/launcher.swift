// Fixture HomeKit launcher.
//
// Starts the Python programs that talk to the bulbs, as child processes, and
// mirrors their lifetime. Talking to the bulbs and announcing to Apple Home
// both use the local network, which macOS gates behind a permission. Running
// them under this app bundle gives macOS one clearly named program to attach
// that permission to, instead of a bare Python binary.
//
//   fixture-homekit               run the Apple Home service
//   fixture-homekit follow apply  run one `fixture` command and exit with it
//
// The Python environment is the one the Neewer Apple Home service uses.

import Foundation

let library = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support", isDirectory: true)
let python = library.appendingPathComponent("NeewerBridge/venv/bin/python")
let programs = library.appendingPathComponent("LightFixture/homekit", isDirectory: true)
let command = Array(CommandLine.arguments.dropFirst())

let service = Process()
service.executableURL = python
if command.isEmpty {
    service.arguments = ["-u", programs.appendingPathComponent("fixture_homekit.py").path]
} else {
    service.arguments = ["-u", programs.appendingPathComponent("fixture_cli.py").path] + command
}
service.terminationHandler = { finished in exit(finished.terminationStatus) }

// launchd stops the agent with SIGTERM; pass it on so the service can shut down cleanly.
signal(SIGTERM, SIG_IGN)
let stopSignal = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
stopSignal.setEventHandler {
    if service.isRunning { service.terminate() } else { exit(0) }
}
stopSignal.resume()

do {
    try service.run()
} catch {
    FileHandle.standardError.write(Data("cannot start \(python.path): \(error.localizedDescription)\n".utf8))
    exit(1)
}
dispatchMain()
