# NearShare for Linux — Product Plan

## 1. Vision

NearShare for Linux brings Google's Quick Share (Nearby Share) protocol to the Ubuntu/GNOME desktop, so files move between an Android phone and a Linux machine as easily as they do between two Android devices — no cables, no cloud accounts, no third-party apps on the phone. Because we speak the wire protocol itself, the same app also interoperates with Windows Quick Share and macOS NearDrop. v1 targets LAN (same WiFi) transfers in both directions, with an experimental "Direct mode" hotspot for when no shared network exists, all wrapped in a native GTK4/libadwaita UI plus a CLI and keyboard shortcut for fast visibility toggling.

## 2. User stories

- **US-1 Receive from phone**: As a Linux user, I select a photo on my Android phone, tap Quick Share, see my laptop in the device list, tap it, confirm a matching 4-digit PIN on the laptop, and the file lands in `~/Downloads`.
- **US-2 Send to phone**: As a Linux user, I pick files in the app, see my phone appear as a nearby device (phone has its Quick Share receive screen open), select it, and the transfer completes with progress shown on both ends.
- **US-3 Toggle visibility fast**: As a privacy-conscious user, I can make my machine visible/invisible in under two seconds — via a UI switch, `nearshare toggle` in a terminal, or a GNOME keyboard shortcut — so I'm only discoverable when I intend to be.
- **US-4 No shared network**: As a user on a phone with no WiFi in common with my laptop (hotel, train), I enable Direct mode, scan the QR code on the laptop screen with my phone, and once the phone joins the laptop's hotspot the normal transfer flow works.
- **US-5 Cross-platform interop**: As a mixed-OS user, I can exchange files with a Windows 11 machine running Quick Share and a Mac running NearDrop, because all three speak the same protocol.

## 3. UX flows

### 3.1 Receive flow (phone → Linux)
1. App is running and visibility is ON; we advertise via mDNS and listen on TCP.
2. User opens the share sheet on Android, picks Quick Share; the Linux device name appears; user taps it.
3. Phone connects; Ukey2 handshake completes; both sides derive the 4-digit PIN token.
4. Linux shows a desktop notification and an in-app dialog: sender name, file name(s), total size, and the 4-digit PIN. Buttons: **Accept** / **Decline**.
5. User visually matches the PIN against the phone's screen and clicks Accept (Decline or a 60 s timeout sends a rejection frame and closes the connection cleanly).
6. Transfer runs with a progress bar (per-file and overall, bytes + %); cancel is available on both ends.
7. On completion: notification with an "Open folder" action; files saved to `~/Downloads` (configurable in preferences); partial files from cancelled/failed transfers are deleted.

### 3.2 Send flow (Linux → phone)
1. User clicks **Send** in the app (or runs `nearshare send <files>`); a GTK file chooser opens (skipped when files came from the CLI).
2. App shows a "Nearby devices" list, populated live from mDNS browsing. Empty-state text explains: "On the phone, open Quick Share receive (or the share sheet) so it becomes discoverable."
3. User clicks the target device; connection + Ukey2 handshake run; the introduction frame (file names/sizes) is sent.
4. Phone shows its accept prompt with PIN; user confirms on the phone.
5. Progress UI on Linux mirrors the receive flow; cancel supported; success/failure clearly reported.

### 3.3 Visibility toggle
- **UI**: A prominent libadwaita switch row on the main window ("Visible to nearby devices"). State changes take effect immediately (mDNS register/unregister + listener up/down).
- **CLI**: `nearshare on|off|toggle|status` talks to the running app over the Unix control socket; `status` prints visibility, device name, and active transfer count; exits non-zero with a clear message if the app isn't running.
- **Keyboard shortcut**: A GNOME custom shortcut bound to `nearshare toggle` (set up by an install script or documented one-liner); toggling fires a desktop notification stating the new state, since GNOME has no tray icon to reflect it.

### 3.4 Direct mode (experimental)
1. User opens the **Direct mode** page and clicks Start; app creates a hotspot via `nmcli` (generated SSID + random WPA2 password).
2. UI shows SSID, password, and a QR code encoding the WiFi credentials (standard `WIFI:` format), plus an "Experimental" badge and a note that the laptop leaves its current WiFi.
3. User scans the QR with the phone camera; phone joins the hotspot; from here the normal receive/send LAN flows apply (mDNS works on the hotspot subnet).
4. Stop button (and app quit) tears the hotspot down and restores the previous WiFi connection; failures from NetworkManager (permissions, unsupported hardware) surface as a readable error dialog, not a stack trace.

## 4. Milestones

- **M1 — Protocol core**: Nearby Share protocol implemented as a UI-free Python library: mDNS advertise + browse, TCP framing, Ukey2 handshake (P-256/HKDF/AES-CBC/HMAC), PIN derivation, paired-key/introduction/transfer frames, send and receive state machines. **Done when** a loopback test (in-process sender ↔ receiver over localhost) transfers files of multiple sizes with correct hashes, and the public API (callbacks for discovery, incoming request, PIN, progress, completion) is stable enough for a UI to build on.
- **M2 — UI receive + send**: GTK4/libadwaita app implementing flows 3.1 and 3.2, including notifications, PIN dialog, device list, progress, cancel, and a preferences page (device name, download folder, visibility default). **Done when** real transfers with a physical Android phone work in both directions.
- **M3 — CLI, shortcut, direct mode**: Unix control socket in the app; `nearshare on|off|toggle|status|send`; GNOME shortcut setup; Direct mode per flow 3.4. **Done when** all CLI verbs work against the running app and a phone with no shared WiFi completes a transfer via Direct mode.
- **M4 — Docs + polish**: README (install, usage, troubleshooting, interop notes), error-message pass, edge-case hardening (timeouts, disconnects, duplicate filenames, disk-full), packaging notes. **Done when** the acceptance checklist below passes end-to-end and a newcomer can install and complete a transfer using only the README.

## 5. Acceptance criteria

### M1 — Protocol core
- [ ] Loopback test: sender and receiver instances in one process transfer 1 KB, 5 MB, and 100 MB files over localhost; SHA-256 of received files matches source.
- [ ] Ukey2 handshake vectors: both sides derive identical keys and identical 4-digit PIN.
- [ ] mDNS: our advertisement is visible to a second zeroconf browser; we can browse and decode another instance's advertisement (endpoint id, device name, type).
- [ ] Receiver rejection path: declining the introduction closes the connection cleanly with the proper response frame; no partial files remain.
- [ ] Mid-transfer disconnect (kill the socket) raises a clean error on both sides and leaves no orphaned threads/sockets.
- [ ] No GTK or CLI imports anywhere in the core package.

### M2 — UI
- [ ] Android → Linux: phone sees the laptop in the share sheet, PIN shown on both matches, Accept saves the file to the configured folder, notification with "Open folder" fires.
- [ ] Decline on Linux causes the phone to report the transfer as declined.
- [ ] Linux → Android: device list shows the phone within 10 s of its receive screen opening; transfer completes; phone-side accept required first.
- [ ] Progress bar advances during a ≥50 MB transfer and reports final size correctly; cancel mid-transfer leaves no partial file and both ends report cancellation.
- [ ] Multi-file send (≥3 files) delivers all files with correct names; duplicate names in the download folder are auto-renamed, not overwritten.
- [ ] Visibility switch OFF: laptop disappears from the phone's share sheet within ~15 s and new inbound connections are refused.

### M3 — CLI / shortcut / direct mode
- [ ] `nearshare on|off|toggle` changes visibility in a running app and prints the new state; `status` output shows visibility and device name; each verb exits 0 on success.
- [ ] All verbs exit non-zero with a helpful message when the app isn't running.
- [ ] `nearshare send a.jpg b.pdf` opens the device picker with those files staged.
- [ ] GNOME shortcut triggers a toggle and a notification announcing the new state.
- [ ] Direct mode: hotspot starts, QR scan joins a phone with no prior shared network, a transfer completes, Stop restores the previous WiFi connection.
- [ ] Direct mode failure (e.g., NetworkManager denies hotspot) shows an actionable error dialog.

### M4 — Docs + polish
- [ ] README covers install on clean Ubuntu 24.04, both transfer flows, CLI, shortcut setup, Direct mode caveats, and a troubleshooting section (firewall/mDNS, "phone can't see laptop", hotspot failures).
- [ ] Interop verified (or explicitly documented as untested) against Windows 11 Quick Share and macOS NearDrop, one transfer each direction where possible.
- [ ] All user-facing errors are human-readable sentences; no raw tracebacks reach dialogs or notifications.
- [ ] App survives: receiver disk full, sender disconnecting mid-transfer, two rapid consecutive transfers, and toggling visibility during an active transfer (active transfer completes).

## 6. Risks & mitigations

- **Protocol drift across Android versions**: Google can change frame details or the mDNS TXT format. *Mitigation*: keep the protocol layer isolated behind the M1 API, pin behavior to the .proto files in `protos/`, test against at least two Android versions, and track NearDrop/community findings for breaking changes.
- **No BLE advertising in v1**: Android normally discovers receivers via BLE before consulting mDNS; without it, the phone only browses mDNS while its Quick Share sheet is open, and sometimes not reliably. *Mitigation*: set expectations in the UI ("keep the share sheet open") and README; this is the top candidate for v2.
- **GNOME has no system tray**: no persistent indicator of visibility state. *Mitigation*: notification on every state change, clear switch state in the app window, `nearshare status` for scripting; optionally document popular AppIndicator extensions without depending on them.
- **Hotspot needs NetworkManager permissions and capable hardware**: `nmcli` hotspot creation can fail on polkit rules or drivers lacking AP mode. *Mitigation*: preflight checks (AP-mode capability, NM reachable) before offering Start, actionable error messages, "Experimental" labeling, and guaranteed restore of the prior connection on stop/crash.
- **mDNS blocked by firewall/VPN**: common cause of "nothing shows up". *Mitigation*: troubleshooting doc section with `ufw` rules and a `status`-level hint when zero peers are ever seen.

## 7. Out of scope for v1

- BLE advertising/scanning (background discovery without the share sheet open).
- Bluetooth as a transfer medium.
- WiFi Direct / P2P group negotiation via wpa_supplicant (Direct mode's nmcli hotspot is the v1 substitute).
- Contact-based visibility ("Contacts" / "Your devices" modes) — v1 is "Everyone"-style visibility only.
- Account certificates / Google account integration.
- Non-GNOME desktops and non-Ubuntu distros as supported targets (may work, untested).
