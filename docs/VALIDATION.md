# Validation

This is an initial implementation, not a hardware-certified control system.

## Automated coverage

Core unit tests cover the non-GTK motion, connection, privacy, gamepad, and OctoEverywhere helpers. The actual GTK dashboard (empty and with sample profiles), connection editor, and gamepad mapper were rendered and inspected at 1280×800 against the pinned upstream checkout. The headless render substituted “no battery” for an unavailable container power-supply device; it did not simulate a connected printer or physical gamepad.

- Motion blocked for printing, paused, unhomed, non-ready, and missing printer state.
- Axis limits and non-finite input rejection.
- Move-panel step propagation, dominant-axis selection, hold-speed scaling, parser-state restoration and `M400`.
- Held-button startup, deflected-stick startup, release during status query, printer switch during status query, loss of focus, stale input, timeout without retry, and remote center-to-repeat behavior.
- URL normalization, IPv6 and reverse-proxy paths, invalid URL rejection, owner-only config permissions.
- Local HTTP fixture verifies Moonraker probing, API-key headers, prefix preservation, and rejection of login redirects.
- Coordinate-mode preservation, speed-override compensation, stick normalization, held-button axis mapping, and remote URL/API-key/App-Connection credential log redaction.
- OctoEverywhere App Connection portal parsing, Shared Connection link parsing (embedded or separate credentials, website pages and non-HTTPS links rejected), and readable 6xx error messages.
- Endpoint selection probes LAN, LAN fallback and OctoEverywhere in parallel, prefers them in that order, and is not held up by a hung `.local` lookup.
- Printer names from Mainsail, Fluidd and Klipper's host name against a local fake Moonraker, including Klipper being down and front-end default names.
- Remote link verification rejects a link that reaches a different printer (OctoEverywhere printer ID, then host name).
- The phone setup page: one-time path, success and failure flows, expiry, request size cap, bad-request shutdown, private-client filter, and the App Connection portal round trip.
- Not covered by automated tests: the GTK dashboard, connection editor and QR panel after this change (no GTK runtime was available), and the switch back to the LAN while connected remotely.

## Hardware acceptance (still required)

1. Install on a fresh Pi 400 / Raspberry Pi OS Lite 64-bit; confirm touchscreen alignment and restart after reboot.
2. Discover two printers and verify the selected name/address before control. Switch using touch, Ctrl+Tab, Alt+number, and a mapped button.
3. With motion clear, home and test each Move-panel distance. Verify analog and mapped-button X/Y/Z directions, then hold a local direction long enough to verify the speed ramp without exceeding the Move-panel XY/Z speed.
4. Release the enable button, unplug the controller, leave Move, switch printers, and lose the network. Confirm no new motion follows, and each requires rearming.
5. Test protected Moonraker with a real API key and reverse-proxy prefix. Link a real OctoEverywhere Shared Connection from a phone via the QR code, confirm the same-printer check (try a different printer's link too), then test LAN-first control, failover to OctoEverywhere when the LAN drops, and the automatic return to the LAN. Check whether OctoEverywhere accepts a returnUrl on the controller's LAN address for App Connections if an App ID becomes available.
5a. Confirm printer names are picked up from Mainsail, Fluidd and the host name, and that dashboard cards show live state and route.
6. Test print pause/resume, macros, heaters, and emergency stop on the intended printer. Remote gamepad steps must require a centered stick between moves.

Network requests can already have reached the printer before a disconnect or button release. A queued bounded move may complete later; the UI cannot retract it.
