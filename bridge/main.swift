// Neewer Bridge: a small Bluetooth pipe for one Neewer light.
//
// This program holds the macOS Bluetooth permission and does only generic work:
// find the light, stay connected, write the bytes it is given, and report replies.
// It knows nothing about Neewer's command format; the `neewer` command builds the
// frames. Keeping the format out of here means experiments never need a rebuild,
// and a rebuild is what makes macOS ask for Bluetooth permission again.
//
// It listens on 127.0.0.1 only. See `GET /` for the routes.

import CoreBluetooth
import Foundation
import Network

let bridgeVersion = "1.0.0"

// MARK: - Logging

private let logFormatter: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter
}()

func log(_ message: String) {
    let line = "\(logFormatter.string(from: Date())) \(message)\n"
    FileHandle.standardOutput.write(Data(line.utf8))
}

// MARK: - Configuration

struct Config {
    var port: UInt16 = 18490
    var namePrefixes: [String] = ["NW-", "NEEWER"]
    var exactName: String?
    var peripheralID: UUID?
    var service = CBUUID(string: "69400001-B5A3-F393-E0A9-E50E24DCCA99")
    var writeCharacteristic = CBUUID(string: "69400002-B5A3-F393-E0A9-E50E24DCCA99")
    var notifyCharacteristic = CBUUID(string: "69400003-B5A3-F393-E0A9-E50E24DCCA99")
    var probeSeconds: Double = 20
    var logWrites = false

    static let directory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/NeewerBridge", isDirectory: true)
    static let file = directory.appendingPathComponent("bridge.json")

    static func load() -> Config {
        var config = Config()
        guard let data = try? Data(contentsOf: file) else {
            log("no config at \(file.path); using defaults")
            return config
        }
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            log("config at \(file.path) is not a JSON object; using defaults")
            return config
        }
        if let value = object["port"] as? Int, (1024...65535).contains(value) {
            config.port = UInt16(value)
        }
        if let value = object["namePrefixes"] as? [String], !value.isEmpty {
            config.namePrefixes = value
        }
        if let value = object["name"] as? String, !value.isEmpty {
            config.exactName = value
        }
        if let value = object["peripheralID"] as? String {
            config.peripheralID = UUID(uuidString: value)
        }
        // CBUUID(string:) raises on malformed input, so validate with UUID first.
        if let value = object["service"] as? String, UUID(uuidString: value) != nil {
            config.service = CBUUID(string: value)
        }
        if let value = object["writeCharacteristic"] as? String, UUID(uuidString: value) != nil {
            config.writeCharacteristic = CBUUID(string: value)
        }
        if let value = object["notifyCharacteristic"] as? String, UUID(uuidString: value) != nil {
            config.notifyCharacteristic = CBUUID(string: value)
        }
        if let value = object["probeSeconds"] as? Double, value >= 5 {
            config.probeSeconds = value
        }
        if let value = object["logWrites"] as? Bool {
            config.logWrites = value
        }
        return config
    }
}

// MARK: - Hex helpers

func hexString(_ data: Data) -> String {
    data.map { String(format: "%02x", $0) }.joined()
}

/// Parses "78 8d 08", "788D08", "0x78,0x8d" and similar. Returns nil if it is not clean hex.
func parseHex(_ text: String) -> Data? {
    var digits = ""
    for token in text.components(separatedBy: CharacterSet(charactersIn: " \t\r\n,:;-")) where !token.isEmpty {
        let lower = token.lowercased()
        digits += lower.hasPrefix("0x") ? String(lower.dropFirst(2)) : lower
    }
    guard !digits.isEmpty, digits.count % 2 == 0 else { return nil }
    var bytes = Data()
    var index = digits.startIndex
    while index < digits.endIndex {
        let next = digits.index(index, offsetBy: 2)
        guard let byte = UInt8(digits[index..<next], radix: 16) else { return nil }
        bytes.append(byte)
        index = next
    }
    return bytes
}

// MARK: - Bluetooth

/// All state lives on `queue`, which is also the CoreBluetooth and HTTP queue.
final class Bridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    private let config: Config
    private let queue: DispatchQueue
    private var central: CBCentralManager!

    private var target: CBPeripheral?
    private var writeChar: CBCharacteristic?
    private var notifyChar: CBCharacteristic?
    private var ready = false
    private var readySince: Date?
    private var rejected = Set<UUID>()
    private var seen: [UUID: (name: String, rssi: Int, at: Date)] = [:]

    private var pending: [(data: Data, type: CBCharacteristicWriteType)] = []
    private var pumping = false
    private var blockedRetries = 0
    private var writeCount = 0
    private var lastWriteAt: Date?
    private var lastWriteError: String?

    private var notifySequence = 0
    private var notifications: [(sequence: Int, at: Date, hex: String)] = []

    private var lastRSSI: Int?
    private var lastRSSIAt: Date?
    private var probeOutstanding = false
    private var probeFailures = 0
    private var probeTimer: DispatchSourceTimer?
    private var disconnectCount = 0
    private let startedAt = Date()

    init(config: Config, queue: DispatchQueue) {
        self.config = config
        self.queue = queue
        super.init()
    }

    func start() {
        central = CBCentralManager(
            delegate: self, queue: queue,
            options: [CBCentralManagerOptionShowPowerAlertKey: false])
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + config.probeSeconds, repeating: config.probeSeconds)
        timer.setEventHandler { [weak self] in self?.probe() }
        timer.resume()
        probeTimer = timer
    }

    // MARK: Finding and connecting

    private func matches(name: String?, identifier: UUID) -> Bool {
        if identifier == config.peripheralID { return true }
        guard let name = name else { return false }
        if let exact = config.exactName { return name == exact }
        return config.namePrefixes.contains { name.hasPrefix($0) }
    }

    private func locate() {
        guard central.state == .poweredOn else { return }
        if target == nil, let id = config.peripheralID,
            let known = central.retrievePeripherals(withIdentifiers: [id]).first
        {
            log("using configured peripheral \(id)")
            adopt(known)
        }
        if target == nil {
            // The light stops advertising while another app on this Mac holds it,
            // but macOS lets a second app share that link.
            let connected = central.retrieveConnectedPeripherals(withServices: [config.service])
            if let shared = connected.first(where: {
                config.exactName == nil || $0.name == config.exactName
            }) {
                log("joining a light this Mac already has connected: \(shared.name ?? "unnamed") \(shared.identifier)")
                adopt(shared)
            }
        }
        if !ready { startScan() }
    }

    private func startScan() {
        guard central.state == .poweredOn, !central.isScanning else { return }
        central.scanForPeripherals(withServices: nil, options: nil)
    }

    private func adopt(_ peripheral: CBPeripheral) {
        target = peripheral
        peripheral.delegate = self
        ready = false
        central.connect(peripheral, options: nil)
    }

    private func reject(_ peripheral: CBPeripheral, reason: String) {
        log("ignoring \(peripheral.name ?? "unnamed") \(peripheral.identifier): \(reason)")
        rejected.insert(peripheral.identifier)
        if target === peripheral { target = nil }
        resetLinkState()
        central.cancelPeripheralConnection(peripheral)
        startScan()
    }

    private func resetLinkState() {
        ready = false
        readySince = nil
        writeChar = nil
        notifyChar = nil
        pending.removeAll()
        pumping = false
        blockedRetries = 0
        probeOutstanding = false
        probeFailures = 0
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        log("bluetooth state: \(Bridge.describe(central.state)), permission: \(Bridge.describe(CBCentralManager.authorization))")
        if central.state == .poweredOn {
            locate()
        } else {
            resetLinkState()
            target = nil
        }
    }

    func centralManager(
        _ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
        advertisementData: [String: Any], rssi RSSI: NSNumber
    ) {
        let name = peripheral.name ?? (advertisementData[CBAdvertisementDataLocalNameKey] as? String)
        if let name = name {
            seen[peripheral.identifier] = (name, RSSI.intValue, Date())
            if seen.count > 80, let oldest = seen.min(by: { $0.value.at < $1.value.at }) {
                seen.removeValue(forKey: oldest.key)
            }
        }
        guard target == nil, !rejected.contains(peripheral.identifier),
            matches(name: name, identifier: peripheral.identifier)
        else { return }
        log("found \(name ?? "unnamed") \(peripheral.identifier) at \(RSSI.intValue) dBm; connecting")
        adopt(peripheral)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard peripheral === target else { return }
        log("connected to \(peripheral.name ?? "unnamed"); looking for the control service")
        peripheral.discoverServices([config.service])
    }

    func centralManager(
        _ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?
    ) {
        log("connect failed: \(error?.localizedDescription ?? "no reason given"); retrying")
        queue.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self = self, self.target === peripheral, self.central.state == .poweredOn else { return }
            self.central.connect(peripheral, options: nil)
        }
    }

    func centralManager(
        _ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?
    ) {
        guard peripheral === target else { return }
        disconnectCount += 1
        log("disconnected (\(error?.localizedDescription ?? "requested")); reconnecting")
        resetLinkState()
        guard central.state == .poweredOn else { return }
        // Ask for a fresh peripheral object: a stale one can report connected yet drop writes.
        let fresh = central.retrievePeripherals(withIdentifiers: [peripheral.identifier]).first ?? peripheral
        adopt(fresh)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard peripheral === target else { return }
        if let error = error {
            log("service discovery failed: \(error.localizedDescription); reconnecting")
            central.cancelPeripheralConnection(peripheral)
            return
        }
        guard let service = peripheral.services?.first(where: { $0.uuid == config.service }) else {
            reject(peripheral, reason: "it has no Neewer control service")
            return
        }
        peripheral.discoverCharacteristics(nil, for: service)
    }

    func peripheral(
        _ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?
    ) {
        guard peripheral === target else { return }
        if let error = error {
            log("characteristic discovery failed: \(error.localizedDescription); reconnecting")
            central.cancelPeripheralConnection(peripheral)
            return
        }
        let characteristics = service.characteristics ?? []
        guard let write = characteristics.first(where: { $0.uuid == config.writeCharacteristic }) else {
            reject(peripheral, reason: "it has no writable control characteristic")
            return
        }
        writeChar = write
        notifyChar = characteristics.first { $0.uuid == config.notifyCharacteristic }
        if let notify = notifyChar,
            notify.properties.contains(.notify) || notify.properties.contains(.indicate)
        {
            peripheral.setNotifyValue(true, for: notify)
        }
        ready = true
        readySince = Date()
        central.stopScan()
        let style = write.properties.contains(.writeWithoutResponse) ? "without response" : "with response"
        log("ready: \(peripheral.name ?? "unnamed") \(peripheral.identifier), writes go \(style), up to \(peripheral.maximumWriteValueLength(for: .withoutResponse)) bytes")
        pump()
    }

    // MARK: Writing

    enum WriteOutcome {
        case queued(type: String, bytes: Int)
        case refused(status: Int, reason: String)
    }

    func write(_ data: Data, forcedType: CBCharacteristicWriteType?) -> WriteOutcome {
        guard central.state == .poweredOn else {
            return .refused(status: 503, reason: "bluetooth is \(Bridge.describe(central.state))")
        }
        guard ready, let peripheral = target, let characteristic = writeChar else {
            return .refused(status: 503, reason: "light is not connected")
        }
        let type: CBCharacteristicWriteType
        if let forced = forcedType {
            type = forced
        } else {
            type = characteristic.properties.contains(.writeWithoutResponse) ? .withoutResponse : .withResponse
        }
        let limit = peripheral.maximumWriteValueLength(for: type)
        guard data.count <= limit else {
            return .refused(status: 400, reason: "frame is \(data.count) bytes; this link carries at most \(limit)")
        }
        pending.append((data, type))
        if pending.count > 64 { pending.removeFirst(pending.count - 64) }
        pump()
        return .queued(type: type == .withoutResponse ? "without-response" : "with-response", bytes: data.count)
    }

    /// Sends queued frames in order, 20 ms apart, so a burst cannot jam the light.
    private func pump() {
        guard !pumping, !pending.isEmpty, ready, let peripheral = target, let characteristic = writeChar,
            peripheral.state == .connected
        else { return }
        let next = pending[0]
        if next.type == .withoutResponse, !peripheral.canSendWriteWithoutResponse, blockedRetries < 5 {
            // Usually cleared by peripheralIsReady; the retry is a backstop.
            blockedRetries += 1
            queue.asyncAfter(deadline: .now() + .milliseconds(50)) { [weak self] in self?.pump() }
            return
        }
        blockedRetries = 0
        pending.removeFirst()
        peripheral.writeValue(next.data, for: characteristic, type: next.type)
        writeCount += 1
        lastWriteAt = Date()
        if config.logWrites { log("wrote \(hexString(next.data))") }
        pumping = true
        queue.asyncAfter(deadline: .now() + .milliseconds(20)) { [weak self] in
            self?.pumping = false
            self?.pump()
        }
    }

    func peripheralIsReady(toSendWriteWithoutResponse peripheral: CBPeripheral) {
        pump()
    }

    func peripheral(
        _ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?
    ) {
        lastWriteError = error?.localizedDescription
        if let error = error { log("write rejected by the light: \(error.localizedDescription)") }
    }

    // MARK: Replies from the light

    func peripheral(
        _ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        if let error = error { log("could not subscribe to replies: \(error.localizedDescription)") }
    }

    func peripheral(
        _ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?
    ) {
        guard error == nil, let value = characteristic.value else { return }
        notifySequence += 1
        notifications.append((notifySequence, Date(), hexString(value)))
        if notifications.count > 100 { notifications.removeFirst(notifications.count - 100) }
        if config.logWrites { log("reply \(hexString(value))") }
    }

    // MARK: Link health

    /// A link can look connected while writes go nowhere, so read the signal
    /// strength on a timer and rebuild the link if the light stops answering.
    private func probe() {
        guard ready, let peripheral = target, peripheral.state == .connected else { return }
        if probeOutstanding {
            probeFailures += 1
            if probeFailures >= 3 {
                log("no answer to \(probeFailures) health checks; rebuilding the link")
                probeOutstanding = false
                probeFailures = 0
                central.cancelPeripheralConnection(peripheral)
                return
            }
        }
        probeOutstanding = true
        peripheral.readRSSI()
    }

    func peripheral(_ peripheral: CBPeripheral, didReadRSSI RSSI: NSNumber, error: Error?) {
        probeOutstanding = false
        if error == nil {
            probeFailures = 0
            lastRSSI = RSSI.intValue
            lastRSSIAt = Date()
        } else {
            probeFailures += 1
        }
    }

    // MARK: Reporting

    func forceReconnect() -> Bool {
        guard let peripheral = target else {
            rejected.removeAll()
            locate()
            return false
        }
        log("reconnect requested")
        central.cancelPeripheralConnection(peripheral)
        return true
    }

    func rescan() {
        rejected.removeAll()
        if central.state == .poweredOn, !ready {
            central.stopScan()
            startScan()
        }
    }

    func status() -> [String: Any] {
        let now = Date()
        func age(_ date: Date?) -> Any { date.map { Int(now.timeIntervalSince($0).rounded()) } ?? NSNull() }
        var result: [String: Any] = [
            "version": bridgeVersion,
            "bluetooth": Bridge.describe(central.state),
            "permission": Bridge.describe(CBCentralManager.authorization),
            "connected": ready,
            "scanning": central.state == .poweredOn && central.isScanning,
            "uptimeSeconds": Int(now.timeIntervalSince(startedAt).rounded()),
            "connectedSeconds": age(readySince),
            "disconnects": disconnectCount,
            "writes": writeCount,
            "lastWriteSecondsAgo": age(lastWriteAt),
            "lastWriteError": lastWriteError ?? NSNull(),
            "queued": pending.count,
            "rssi": lastRSSI ?? NSNull(),
            "rssiSecondsAgo": age(lastRSSIAt),
            "missedHealthChecks": probeFailures,
            "replies": notifySequence,
            "lastReply": notifications.last?.hex ?? NSNull(),
        ]
        if let peripheral = target {
            result["name"] = peripheral.name ?? NSNull()
            result["identifier"] = peripheral.identifier.uuidString
            if ready { result["maxFrameBytes"] = peripheral.maximumWriteValueLength(for: .withoutResponse) }
        }
        return result
    }

    func replies(after sequence: Int) -> [[String: Any]] {
        let now = Date()
        return notifications.filter { $0.sequence > sequence }.map {
            ["sequence": $0.sequence, "secondsAgo": Int(now.timeIntervalSince($0.at).rounded()), "hex": $0.hex]
        }
    }

    func nearby() -> [[String: Any]] {
        let now = Date()
        return seen.sorted { $0.value.rssi > $1.value.rssi }.map {
            [
                "name": $0.value.name, "identifier": $0.key.uuidString, "rssi": $0.value.rssi,
                "secondsAgo": Int(now.timeIntervalSince($0.value.at).rounded()),
            ]
        }
    }

    static func describe(_ state: CBManagerState) -> String {
        switch state {
        case .poweredOn: return "on"
        case .poweredOff: return "off"
        case .unauthorized: return "not-permitted"
        case .unsupported: return "unsupported"
        case .resetting: return "resetting"
        case .unknown: return "starting"
        @unknown default: return "unknown"
        }
    }

    static func describe(_ authorization: CBManagerAuthorization) -> String {
        switch authorization {
        case .allowedAlways: return "allowed"
        case .denied: return "denied"
        case .restricted: return "restricted"
        case .notDetermined: return "not-asked-yet"
        @unknown default: return "unknown"
        }
    }
}

// MARK: - Minimal HTTP

struct HTTPRequest {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]
    let body: Data
}

struct HTTPResponse {
    let status: Int
    let contentType: String
    let body: Data

    static func json(_ status: Int, _ object: Any) -> HTTPResponse {
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
        return HTTPResponse(status: status, contentType: "application/json", body: data + Data("\n".utf8))
    }

    static func failure(_ status: Int, _ reason: String) -> HTTPResponse {
        json(status, ["ok": false, "error": reason])
    }

    static func text(_ status: Int, _ text: String) -> HTTPResponse {
        HTTPResponse(status: status, contentType: "text/plain; charset=utf-8", body: Data(text.utf8))
    }

    func serialized() -> Data {
        let reasons = [
            200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 413: "Payload Too Large", 503: "Service Unavailable",
        ]
        let head =
            "HTTP/1.1 \(status) \(reasons[status] ?? "OK")\r\nContent-Type: \(contentType)\r\n"
            + "Content-Length: \(body.count)\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n"
        return Data(head.utf8) + body
    }
}

enum ParseResult {
    case incomplete
    case malformed(String)
    case complete(HTTPRequest)
}

func parseRequest(_ buffer: Data) -> ParseResult {
    guard let terminator = buffer.range(of: Data("\r\n\r\n".utf8)) else {
        return buffer.count > 16384 ? .malformed("headers too large") : .incomplete
    }
    guard let head = String(data: buffer.subdata(in: buffer.startIndex..<terminator.lowerBound), encoding: .utf8)
    else { return .malformed("headers are not UTF-8") }
    var lines = head.components(separatedBy: "\r\n")
    let requestLine = lines.removeFirst().split(separator: " ", omittingEmptySubsequences: true)
    guard requestLine.count >= 2 else { return .malformed("bad request line") }
    var headers: [String: String] = [:]
    for line in lines {
        guard let colon = line.firstIndex(of: ":") else { continue }
        headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
    }
    guard let length = Int(headers["content-length"] ?? "0"), length >= 0, length <= 8192 else {
        return .malformed("body too large")
    }
    let available = buffer.endIndex - terminator.upperBound
    guard available >= length else { return .incomplete }
    let body = buffer.subdata(in: terminator.upperBound..<(terminator.upperBound + length))
    let target = String(requestLine[1])
    let components = URLComponents(string: "http://localhost" + target)
    var query: [String: String] = [:]
    for item in components?.queryItems ?? [] { query[item.name] = item.value ?? "" }
    return .complete(
        HTTPRequest(
            method: requestLine[0].uppercased(), path: components?.path ?? target, query: query,
            headers: headers, body: body))
}

final class HTTPServer {
    private let listener: NWListener
    private let queue: DispatchQueue
    private let handler: (HTTPRequest) -> HTTPResponse

    init(port: UInt16, queue: DispatchQueue, handler: @escaping (HTTPRequest) -> HTTPResponse) throws {
        let parameters = NWParameters.tcp
        parameters.allowLocalEndpointReuse = true
        // Loopback only: nothing off this Mac can reach the bridge.
        parameters.requiredLocalEndpoint = NWEndpoint.hostPort(
            host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!)
        listener = try NWListener(using: parameters)
        self.queue = queue
        self.handler = handler
    }

    func start() {
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready:
                log("listening on 127.0.0.1")
            case .failed(let error):
                log("cannot listen: \(error.localizedDescription)")
                exit(1)
            default:
                break
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            guard let self = self else { return }
            connection.start(queue: self.queue)
            // Nothing legitimate takes this long; do not let a stuck client pile up.
            self.queue.asyncAfter(deadline: .now() + 10) { connection.cancel() }
            self.read(connection, buffer: Data())
        }
        listener.start(queue: queue)
    }

    private func read(_ connection: NWConnection, buffer: Data) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 16384) { [weak self] data, _, finished, error in
            guard let self = self else { return }
            var buffer = buffer
            if let data = data { buffer.append(data) }
            switch parseRequest(buffer) {
            case .complete(let request):
                self.reply(connection, self.handler(request))
            case .malformed(let reason):
                self.reply(connection, .failure(400, reason))
            case .incomplete:
                if finished || error != nil || buffer.count > 32768 {
                    connection.cancel()
                } else {
                    self.read(connection, buffer: buffer)
                }
            }
        }
    }

    private func reply(_ connection: NWConnection, _ response: HTTPResponse) {
        connection.send(content: response.serialized(), completion: .contentProcessed { _ in connection.cancel() })
    }
}

// MARK: - Routes

let usage = """
    Neewer Bridge \(bridgeVersion) (Bluetooth pipe for one Neewer light)

    GET  /status     link state, signal strength, counters
    POST /write      body: frame as hex, sent as-is. ?checksum=1 appends the
                     Neewer checksum byte; ?type=response|noresponse forces the write type
    GET  /replies    frames the light sent back. ?after=N for newer than N
    GET  /nearby     named Bluetooth devices seen while scanning
    POST /reconnect  drop and rebuild the link
    POST /rescan     forget rejected devices and scan again

    """

func route(_ request: HTTPRequest, bridge: Bridge) -> HTTPResponse {
    // A web page in a browser on this Mac could otherwise post to this port.
    if request.headers["origin"] != nil {
        return .failure(403, "requests from web pages are not accepted")
    }
    switch (request.method, request.path) {
    case ("GET", "/"):
        return .text(200, usage)
    case ("GET", "/status"):
        return .json(200, bridge.status())
    case ("GET", "/replies"):
        return .json(200, ["replies": bridge.replies(after: Int(request.query["after"] ?? "0") ?? 0)])
    case ("GET", "/nearby"):
        return .json(200, ["devices": bridge.nearby()])
    case ("POST", "/reconnect"):
        return .json(200, ["ok": true, "wasConnected": bridge.forceReconnect()])
    case ("POST", "/rescan"):
        bridge.rescan()
        return .json(200, ["ok": true])
    case ("POST", "/write"):
        let text = String(data: request.body, encoding: .utf8) ?? ""
        guard var frame = parseHex(text.isEmpty ? (request.query["hex"] ?? "") : text) else {
            return .failure(400, "body must be the frame as hex, for example 788d08aabbccddeeff8101")
        }
        if ["1", "true", "add"].contains(request.query["checksum"] ?? "") {
            frame.append(UInt8(frame.reduce(0) { ($0 + Int($1)) & 0xFF }))
        }
        var forced: CBCharacteristicWriteType?
        switch request.query["type"] {
        case "response": forced = .withResponse
        case "noresponse": forced = .withoutResponse
        default: forced = nil
        }
        switch bridge.write(frame, forcedType: forced) {
        case .queued(let type, let bytes):
            return .json(200, ["ok": true, "sent": hexString(frame), "bytes": bytes, "type": type])
        case .refused(let status, let reason):
            return .failure(status, reason)
        }
    case (_, "/"), (_, "/status"), (_, "/replies"), (_, "/nearby"), (_, "/reconnect"), (_, "/rescan"), (_, "/write"):
        return .failure(405, "wrong method for \(request.path)")
    default:
        return .failure(404, "no such route; GET / lists them")
    }
}

// MARK: - Main

let config = Config.load()
let mainQueue = DispatchQueue(label: "local.neewer-bridge")
let bridge = Bridge(config: config, queue: mainQueue)

log("Neewer Bridge \(bridgeVersion) starting; port \(config.port)")
do {
    let server = try HTTPServer(port: config.port, queue: mainQueue) { route($0, bridge: bridge) }
    server.start()
    mainQueue.async { bridge.start() }
    withExtendedLifetime(server) { dispatchMain() }
} catch {
    log("cannot open 127.0.0.1:\(config.port): \(error.localizedDescription)")
    exit(1)
}
