// nearshare-ble: Windows Bluetooth LE helper for NearShare.
//
// Python on Windows (MSYS2 UCRT64) cannot reach WinRT, so BLE advertising and
// scanning live in this small C++/WinRT console program. nearshare/core/
// ble_windows.py spawns it and talks to it over stdin/stdout, one JSON object
// per line, UTF-8, every output line flushed immediately.
//
// Startup (stdout):
//   {"event":"ready","peripheral":true,"central":true}
//   {"event":"unavailable","message":"..."}            then exit code 2
//
// Commands (stdin):
//   {"cmd":"advertise","id":"a1",
//    "service_data":{"fe2c":"<hex>"}, "manufacturer_data":{"76":"<hex>"}}
//   {"cmd":"stop","id":"a1"}
//   {"cmd":"scan","uuid":"fe2c"}
//   {"cmd":"scan_stop"}
//   EOF on stdin: stop everything and exit 0.
//
// Events (stdout):
//   {"event":"advertise","id":"a1","status":"Started","error":""}
//   {"event":"scan","status":"Started|Stopped","error":""}
//   {"event":"seen","uuid":"fe2c","rssi":-60}          (at most 1/s)
//   {"event":"error","id":"a1","message":"..."}        (id/cmd when known)
//
// Build with windows/ble/build.cmd (MSVC, C++17).

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Data.Json.h>
#include <winrt/Windows.Devices.Bluetooth.h>
#include <winrt/Windows.Devices.Bluetooth.Advertisement.h>
#include <winrt/Windows.Storage.Streams.h>

#include <fcntl.h>
#include <io.h>

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace bt = winrt::Windows::Devices::Bluetooth;
namespace adv = winrt::Windows::Devices::Bluetooth::Advertisement;
namespace json = winrt::Windows::Data::Json;
namespace streams = winrt::Windows::Storage::Streams;

namespace {

using Clock = std::chrono::steady_clock;

constexpr std::uint8_t kAdTypeServiceData16 = 0x16;
constexpr auto kStopWait = std::chrono::seconds(3);
constexpr auto kSeenInterval = std::chrono::seconds(1);

// ---------------------------------------------------------------- output --

std::mutex g_out_mutex;

void emit_line(std::string const& line) {
    std::lock_guard<std::mutex> guard(g_out_mutex);
    std::fwrite(line.data(), 1, line.size(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

std::string quote(std::string const& s) {
    std::string out = "\"";
    for (unsigned char c : s) {
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:
            if (c < 0x20) {
                char buf[8];
                std::snprintf(buf, sizeof buf, "\\u%04x", c);
                out += buf;
            } else {
                out += static_cast<char>(c);  // UTF-8 passes through
            }
        }
    }
    out += '"';
    return out;
}

void emit_error(std::string const& message, std::string const& id = {},
                std::string const& cmd = {}) {
    std::string line = "{\"event\":\"error\"";
    if (!id.empty()) line += ",\"id\":" + quote(id);
    if (!cmd.empty()) line += ",\"cmd\":" + quote(cmd);
    line += ",\"message\":" + quote(message) + "}";
    emit_line(line);
}

std::string describe(winrt::hresult_error const& e) {
    std::string msg = winrt::to_string(e.message());
    while (!msg.empty() && (msg.back() == '\n' || msg.back() == '\r' ||
                            msg.back() == ' ' || msg.back() == '.'))
        msg.pop_back();
    char code[16];
    std::snprintf(code, sizeof code, "0x%08X",
                  static_cast<unsigned>(e.code().value));
    return std::string(code) + ": " + msg;
}

// ----------------------------------------------------------------- names --

std::string status_name(adv::BluetoothLEAdvertisementPublisherStatus s) {
    switch (s) {
    case adv::BluetoothLEAdvertisementPublisherStatus::Created:  return "Created";
    case adv::BluetoothLEAdvertisementPublisherStatus::Waiting:  return "Waiting";
    case adv::BluetoothLEAdvertisementPublisherStatus::Started:  return "Started";
    case adv::BluetoothLEAdvertisementPublisherStatus::Stopping: return "Stopping";
    case adv::BluetoothLEAdvertisementPublisherStatus::Stopped:  return "Stopped";
    case adv::BluetoothLEAdvertisementPublisherStatus::Aborted:  return "Aborted";
    }
    return "Unknown";
}

// BluetoothError::Success maps to "" ("no error").
std::string error_name(bt::BluetoothError e) {
    switch (e) {
    case bt::BluetoothError::Success:              return "";
    case bt::BluetoothError::RadioNotAvailable:    return "RadioNotAvailable";
    case bt::BluetoothError::ResourceInUse:        return "ResourceInUse";
    case bt::BluetoothError::DeviceNotConnected:   return "DeviceNotConnected";
    case bt::BluetoothError::OtherError:           return "OtherError";
    case bt::BluetoothError::DisabledByPolicy:     return "DisabledByPolicy";
    case bt::BluetoothError::NotSupported:         return "NotSupported";
    case bt::BluetoothError::DisabledByUser:       return "DisabledByUser";
    case bt::BluetoothError::ConsentRequired:      return "ConsentRequired";
    case bt::BluetoothError::TransportNotSupported:return "TransportNotSupported";
    }
    return "Error" + std::to_string(static_cast<int>(e));
}

// ------------------------------------------------------------------ hex ---

int hex_digit(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

bool parse_hex(std::string const& s, std::vector<std::uint8_t>& out) {
    if (s.size() % 2 != 0) return false;
    out.clear();
    out.reserve(s.size() / 2);
    for (std::size_t i = 0; i < s.size(); i += 2) {
        int hi = hex_digit(s[i]), lo = hex_digit(s[i + 1]);
        if (hi < 0 || lo < 0) return false;
        out.push_back(static_cast<std::uint8_t>(hi * 16 + lo));
    }
    return true;
}

// "fe2c" or "0xfe2c" -> 0xFE2C
bool parse_uuid16(std::string s, std::uint16_t& out) {
    if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X'))
        s.erase(0, 2);
    if (s.size() != 4) return false;
    unsigned v = 0;
    for (char c : s) {
        int d = hex_digit(c);
        if (d < 0) return false;
        v = v * 16 + static_cast<unsigned>(d);
    }
    out = static_cast<std::uint16_t>(v);
    return true;
}

// Decimal ("76") or 0x-prefixed hex ("0x004C").
bool parse_company(std::string const& s, std::uint16_t& out) {
    if (s.empty()) return false;
    unsigned long v = 0;
    if (s.size() > 2 && s[0] == '0' && (s[1] == 'x' || s[1] == 'X')) {
        for (std::size_t i = 2; i < s.size(); ++i) {
            int d = hex_digit(s[i]);
            if (d < 0) return false;
            v = v * 16 + static_cast<unsigned long>(d);
            if (v > 0xFFFF) return false;
        }
    } else {
        for (char c : s) {
            if (c < '0' || c > '9') return false;
            v = v * 10 + static_cast<unsigned long>(c - '0');
            if (v > 0xFFFF) return false;
        }
    }
    out = static_cast<std::uint16_t>(v);
    return true;
}

streams::IBuffer to_buffer(std::vector<std::uint8_t> const& bytes) {
    streams::DataWriter writer;
    writer.WriteBytes(bytes);
    return writer.DetachBuffer();
}

std::vector<std::uint8_t> to_bytes(streams::IBuffer const& buffer) {
    streams::DataReader reader = streams::DataReader::FromBuffer(buffer);
    std::vector<std::uint8_t> bytes(reader.UnconsumedBufferLength());
    if (!bytes.empty()) reader.ReadBytes(bytes);
    return bytes;
}

// ------------------------------------------------------------------ json --

bool get_string(json::JsonObject const& o, wchar_t const* key,
                std::string& out) {
    if (!o.HasKey(key)) return false;
    json::IJsonValue v = o.GetNamedValue(key);
    if (v.ValueType() != json::JsonValueType::String) return false;
    out = winrt::to_string(v.GetString());
    return true;
}

bool get_object(json::JsonObject const& o, wchar_t const* key,
                json::JsonObject& out) {
    if (!o.HasKey(key)) return false;
    json::IJsonValue v = o.GetNamedValue(key);
    if (v.ValueType() != json::JsonValueType::Object) return false;
    out = v.GetObject();
    return true;
}

// ------------------------------------------------------------ publishers --

struct Publisher {
    std::string id;
    adv::BluetoothLEAdvertisementPublisher publisher{nullptr};
    winrt::event_token token{};

    std::mutex m;                       // guards everything below
    std::condition_variable cv;
    adv::BluetoothLEAdvertisementPublisherStatus status =
        adv::BluetoothLEAdvertisementPublisherStatus::Created;
    std::string error;
    bool quiet = false;                 // swallow events (stop/replace)

    bool terminal_locked() const {
        return status == adv::BluetoothLEAdvertisementPublisherStatus::Stopped ||
               status == adv::BluetoothLEAdvertisementPublisherStatus::Aborted;
    }
};

void emit_advertise(std::string const& id, std::string const& status,
                    std::string const& error) {
    emit_line("{\"event\":\"advertise\",\"id\":" + quote(id) +
              ",\"status\":" + quote(status) +
              ",\"error\":" + quote(error) + "}");
}

// Stops the publisher, waits for a terminal status, detaches the handler.
// Returns the final {status, error}. Runs on the command thread only.
std::pair<std::string, std::string> shut_down(Publisher& p) {
    {
        std::lock_guard<std::mutex> guard(p.m);
        p.quiet = true;
    }
    try {
        p.publisher.Stop();
    } catch (winrt::hresult_error const&) {
        // Already stopped/aborted/never started: the status below says so.
    }
    std::unique_lock<std::mutex> lock(p.m);
    p.cv.wait_for(lock, kStopWait, [&] { return p.terminal_locked(); });
    std::string status = status_name(p.status);
    std::string error = p.error;
    lock.unlock();
    try {
        p.publisher.StatusChanged(p.token);
    } catch (winrt::hresult_error const&) {
    }
    return {status, error};
}

// --------------------------------------------------------------- scanning -

struct Scanner {
    adv::BluetoothLEAdvertisementWatcher watcher{nullptr};
    winrt::event_token received_token{};
    winrt::event_token stopped_token{};
    std::uint16_t uuid16 = 0;
    std::string uuid_text;

    std::mutex m;
    std::condition_variable cv;
    bool stopped = false;               // Stopped event observed
    bool stopping = false;              // scan_stop requested
    bool have_last = false;
    Clock::time_point last_seen{};
};

// --------------------------------------------------------------- the app --

class App {
public:
    explicit App(bt::BluetoothAdapter adapter) : adapter_(std::move(adapter)) {}

    void ready() {
        emit_line(std::string("{\"event\":\"ready\",\"peripheral\":") +
                  (adapter_.IsPeripheralRoleSupported() ? "true" : "false") +
                  ",\"central\":" +
                  (adapter_.IsCentralRoleSupported() ? "true" : "false") + "}");
    }

    void handle_line(std::string line) {
        while (!line.empty() && (line.back() == '\r' || line.back() == '\n'))
            line.pop_back();
        if (line.find_first_not_of(" \t") == std::string::npos) return;

        json::JsonObject obj{nullptr};
        bool parsed = false;
        try {
            parsed = json::JsonObject::TryParse(winrt::to_hstring(line), obj);
        } catch (winrt::hresult_error const&) {
        }
        std::string cmd;
        if (!parsed || !get_string(obj, L"cmd", cmd)) {
            emit_error("bad command");
            return;
        }
        try {
            if (cmd == "advertise") cmd_advertise(obj);
            else if (cmd == "stop") cmd_stop(obj);
            else if (cmd == "scan") cmd_scan(obj);
            else if (cmd == "scan_stop") cmd_scan_stop();
            else emit_error("bad command");
        } catch (winrt::hresult_error const& e) {
            emit_error(describe(e));
        } catch (std::exception const& e) {
            emit_error(e.what());
        }
    }

    void shutdown() {
        for (auto& entry : publishers_) shut_down(*entry.second);
        publishers_.clear();
        stop_scanner();
    }

private:
    // ---- advertise ----

    void cmd_advertise(json::JsonObject const& obj) {
        std::string id;
        if (!get_string(obj, L"id", id) || id.empty()) {
            emit_error("bad command", {}, "advertise");
            return;
        }

        // A second advertise with the same id replaces the first, silently.
        auto old = publishers_.find(id);
        if (old != publishers_.end()) {
            shut_down(*old->second);
            publishers_.erase(old);
        }

        auto p = std::make_shared<Publisher>();
        p->id = id;
        try {
            p->publisher = adv::BluetoothLEAdvertisementPublisher();
            auto advert = p->publisher.Advertisement();
            bool any = false;

            json::JsonObject sd{nullptr};
            if (get_object(obj, L"service_data", sd)) {
                for (auto const& kv : sd) {
                    std::string key = winrt::to_string(kv.Key());
                    std::uint16_t uuid;
                    std::vector<std::uint8_t> payload;
                    json::IJsonValue v = kv.Value();
                    if (!parse_uuid16(key, uuid) ||
                        v.ValueType() != json::JsonValueType::String ||
                        !parse_hex(winrt::to_string(v.GetString()), payload)) {
                        emit_error("bad service_data entry for '" + key + "'",
                                   id);
                        return;
                    }
                    // Type 0x16: 16-bit UUID (little-endian) + payload.
                    std::vector<std::uint8_t> data;
                    data.push_back(static_cast<std::uint8_t>(uuid & 0xFF));
                    data.push_back(static_cast<std::uint8_t>(uuid >> 8));
                    data.insert(data.end(), payload.begin(), payload.end());
                    advert.DataSections().Append(adv::BluetoothLEAdvertisementDataSection(
                        kAdTypeServiceData16, to_buffer(data)));
                    any = true;
                }
            }

            json::JsonObject md{nullptr};
            if (get_object(obj, L"manufacturer_data", md)) {
                for (auto const& kv : md) {
                    std::string key = winrt::to_string(kv.Key());
                    std::uint16_t company;
                    std::vector<std::uint8_t> payload;
                    json::IJsonValue v = kv.Value();
                    if (!parse_company(key, company) ||
                        v.ValueType() != json::JsonValueType::String ||
                        !parse_hex(winrt::to_string(v.GetString()), payload)) {
                        emit_error("bad manufacturer_data entry for '" + key + "'",
                                   id);
                        return;
                    }
                    advert.ManufacturerData().Append(
                        adv::BluetoothLEManufacturerData(company, to_buffer(payload)));
                    any = true;
                }
            }

            if (!any) {
                emit_error("advertise needs service_data or manufacturer_data",
                           id);
                return;
            }

            std::weak_ptr<Publisher> weak = p;
            p->token = p->publisher.StatusChanged(
                [weak](adv::BluetoothLEAdvertisementPublisher const&,
                       adv::BluetoothLEAdvertisementPublisherStatusChangedEventArgs const& args) {
                    auto self = weak.lock();
                    if (!self) return;
                    std::lock_guard<std::mutex> guard(self->m);
                    self->status = args.Status();
                    self->error = error_name(args.Error());
                    if (!self->quiet)
                        emit_advertise(self->id, status_name(self->status),
                                       self->error);
                    self->cv.notify_all();
                });
            publishers_[id] = p;
            try {
                p->publisher.Start();
            } catch (...) {
                publishers_.erase(id);
                try { p->publisher.StatusChanged(p->token); } catch (...) {}
                throw;
            }
        } catch (winrt::hresult_error const& e) {
            emit_error(describe(e), id);
        }
    }

    void cmd_stop(json::JsonObject const& obj) {
        std::string id;
        if (!get_string(obj, L"id", id)) {
            emit_error("bad command", {}, "stop");
            return;
        }
        auto it = publishers_.find(id);
        if (it == publishers_.end()) {
            // Unknown or already gone: stopping is idempotent.
            emit_advertise(id, "Stopped", "");
            return;
        }
        auto p = it->second;
        publishers_.erase(it);
        auto result = shut_down(*p);
        emit_advertise(id, result.first, result.second);
    }

    // ---- scan ----

    void cmd_scan(json::JsonObject const& obj) {
        std::string uuid_text;
        std::uint16_t uuid;
        if (!get_string(obj, L"uuid", uuid_text) ||
            !parse_uuid16(uuid_text, uuid)) {
            emit_error("bad command", {}, "scan");
            return;
        }
        stop_scanner();  // a new scan replaces the old one

        auto s = std::make_shared<Scanner>();
        s->uuid16 = uuid;
        char norm[8];
        std::snprintf(norm, sizeof norm, "%04x", uuid);
        s->uuid_text = norm;
        try {
            s->watcher = adv::BluetoothLEAdvertisementWatcher();
            s->watcher.ScanningMode(adv::BluetoothLEScanningMode::Passive);
            // BlueZ on current kernels sends its beacons as extended
            // advertisements when the controller can; without this the
            // watcher only reports legacy ones and never sees them.
            try {
                s->watcher.AllowExtendedAdvertisements(true);
            } catch (winrt::hresult_error const&) {
                // Older Windows 10 builds: legacy advertisements only.
            }

            std::weak_ptr<Scanner> weak = s;
            s->received_token = s->watcher.Received(
                [weak](adv::BluetoothLEAdvertisementWatcher const&,
                       adv::BluetoothLEAdvertisementReceivedEventArgs const& args) {
                    auto self = weak.lock();
                    if (!self) return;
                    for (auto const& section :
                         args.Advertisement().GetSectionsByType(kAdTypeServiceData16)) {
                        auto data = to_bytes(section.Data());
                        if (data.size() < 2) continue;
                        std::uint16_t got = static_cast<std::uint16_t>(
                            data[0] | (data[1] << 8));
                        if (got != self->uuid16) continue;
                        std::lock_guard<std::mutex> guard(self->m);
                        if (self->stopping) return;
                        auto now = Clock::now();
                        if (self->have_last && now - self->last_seen < kSeenInterval)
                            return;
                        self->have_last = true;
                        self->last_seen = now;
                        emit_line("{\"event\":\"seen\",\"uuid\":" +
                                  quote(self->uuid_text) + ",\"rssi\":" +
                                  std::to_string(args.RawSignalStrengthInDBm()) + "}");
                        return;
                    }
                });
            s->stopped_token = s->watcher.Stopped(
                [weak](adv::BluetoothLEAdvertisementWatcher const&,
                       adv::BluetoothLEAdvertisementWatcherStoppedEventArgs const& args) {
                    auto self = weak.lock();
                    if (!self) return;
                    std::lock_guard<std::mutex> guard(self->m);
                    self->stopped = true;
                    emit_line("{\"event\":\"scan\",\"status\":\"Stopped\",\"error\":" +
                              quote(error_name(args.Error())) + "}");
                    self->cv.notify_all();
                });
            scanner_ = s;
            try {
                s->watcher.Start();
            } catch (...) {
                scanner_.reset();
                detach_scanner(*s);
                throw;
            }
            emit_line("{\"event\":\"scan\",\"status\":\"Started\",\"error\":\"\"}");
        } catch (winrt::hresult_error const& e) {
            emit_error(describe(e), {}, "scan");
        }
    }

    void cmd_scan_stop() { stop_scanner(); }

    void detach_scanner(Scanner& s) {
        try { s.watcher.Received(s.received_token); } catch (...) {}
        try { s.watcher.Stopped(s.stopped_token); } catch (...) {}
    }

    void stop_scanner() {
        if (!scanner_) return;
        auto s = std::move(scanner_);
        scanner_.reset();
        {
            std::lock_guard<std::mutex> guard(s->m);
            s->stopping = true;
        }
        try {
            s->watcher.Stop();
        } catch (winrt::hresult_error const&) {
        }
        bool saw_stopped;
        {
            std::unique_lock<std::mutex> lock(s->m);
            s->cv.wait_for(lock, kStopWait, [&] { return s->stopped; });
            saw_stopped = s->stopped;
        }
        detach_scanner(*s);
        if (!saw_stopped)
            emit_line("{\"event\":\"scan\",\"status\":\"Stopped\",\"error\":\"\"}");
    }

    bt::BluetoothAdapter adapter_;
    std::map<std::string, std::shared_ptr<Publisher>> publishers_;
    std::shared_ptr<Scanner> scanner_;
};

int unavailable(std::string const& why) {
    emit_line("{\"event\":\"unavailable\",\"message\":" + quote(why) + "}");
    return 2;
}

}  // namespace

int main() {
    // Raw bytes both ways: no CRLF translation, no Ctrl-Z EOF.
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);

    winrt::init_apartment(winrt::apartment_type::multi_threaded);

    bt::BluetoothAdapter adapter{nullptr};
    try {
        adapter = bt::BluetoothAdapter::GetDefaultAsync().get();
    } catch (winrt::hresult_error const& e) {
        return unavailable("could not query the Bluetooth adapter: " +
                           describe(e));
    }
    if (!adapter) return unavailable("no Bluetooth adapter found");
    try {
        if (!adapter.IsLowEnergySupported())
            return unavailable("the Bluetooth adapter does not support LE");
    } catch (winrt::hresult_error const& e) {
        return unavailable("could not query the Bluetooth adapter: " +
                           describe(e));
    }

    App app(adapter);
    app.ready();

    std::string line;
    while (std::getline(std::cin, line)) app.handle_line(line);

    app.shutdown();
    return 0;
}
