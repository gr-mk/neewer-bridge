// Neewer HomeKit launcher.
//
// Starts the Python HomeKit service as a child process and mirrors its lifetime.
// The service needs to announce itself on the local network, which macOS gates
// behind a permission. Running it under this app bundle gives macOS one clearly
// named program to attach that permission to, instead of a bare Python binary.

import Foundation

let support = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/NeewerBridge", isDirectory: true)
let python = support.appendingPathComponent("venv/bin/python")
let script = support.appendingPathComponent("homekit/neewer_homekit.py")

let service = Process()
service.executableURL = python
service.arguments = ["-u", script.path]
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
