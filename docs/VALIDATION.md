# Validation

This is an initial implementation, not a hardware-certified control system.

## Automated coverage

26 core tests passed in the development container. The actual GTK dashboard (empty and with sample profiles), connection editor, and gamepad mapper were rendered and inspected at 1280×800 against the pinned upstream checkout. The headless render substituted “no battery” for an unavailable container power-supply device; it did not simulate a connected printer or physical gamepad.

- Motion blocked for printing, paused, unhomed, non-ready, and missing printer state.
- Axis limits and non-finite input rejection.
- Bounded local/remote distances, dominant-axis selection, parser-state restoration and `M400`.
- Held-button startup, deflected-stick startup, release during status query, printer switch during status query, loss of focus, stale input, timeout without retry, and remote center-to-repeat behavior.
- URL normalization, IPv6 and reverse-proxy paths, invalid URL rejection, owner-only config permissions.
- Local HTTP fixture verifies Moonraker probing, API-key headers, prefix preservation, and rejection of login redirects.
- Coordinate-mode preservation, speed-override compensation, stick normalization, and remote URL/API-key log redaction.

## Hardware acceptance (still required)

1. Install on a fresh Pi 400 / Raspberry Pi OS Lite 64-bit; confirm touchscreen alignment and restart after reboot.
2. Discover two printers and verify the selected name/address before control. Switch using touch, Ctrl+Tab, Alt+number, and a mapped button.
3. With motion clear, home and test a single 0.2–1 mm jog at low speed. Verify all directions and the selected printer.
4. Release the enable button, unplug the controller, leave Move, switch printers, and lose the network. Confirm no new motion follows, and each requires rearming.
5. Test protected Moonraker with a real API key, a reverse-proxy prefix, and a valid OctoEverywhere custom app URL. Confirm both HTTPS requests and WSS KlipperScreen controls.
6. Test print pause/resume, macros, heaters, and emergency stop on the intended printer. Remote gamepad steps must require a centered stick between moves.

Network requests can already have reached the printer before a disconnect or button release. A queued bounded move may complete later; the UI cannot retract it.
