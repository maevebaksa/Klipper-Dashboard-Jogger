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
- OctoEverywhere App Connection portal parsing and LAN-first endpoint selection are covered by unit tests; the hosted authorization portal still requires an assigned production App ID and on-device validation.

## Hardware acceptance (still required)

1. Install on a fresh Pi 400 / Raspberry Pi OS Lite 64-bit; confirm touchscreen alignment and restart after reboot.
2. Discover two printers and verify the selected name/address before control. Switch using touch, Ctrl+Tab, Alt+number, and a mapped button.
3. With motion clear, home and test each Move-panel distance. Verify analog and mapped-button X/Y/Z directions, then hold a local direction long enough to verify the speed ramp without exceeding the Move-panel XY/Z speed.
4. Release the enable button, unplug the controller, leave Move, switch printers, and lose the network. Confirm no new motion follows, and each requires rearming.
5. Test protected Moonraker with a real API key and reverse-proxy prefix. With an assigned KlipperController OctoEverywhere App ID, complete the embedded App Connection portal, verify automatic local printer-ID detection, then test LAN-first and remote fallback HTTPS/WSS control.
6. Test print pause/resume, macros, heaters, and emergency stop on the intended printer. Remote gamepad steps must require a centered stick between moves.

Network requests can already have reached the printer before a disconnect or button release. A queued bounded move may complete later; the UI cannot retract it.
