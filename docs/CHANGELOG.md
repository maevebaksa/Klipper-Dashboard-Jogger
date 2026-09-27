# Changelog

## 2026-09-27: Glanceable dashboard, Manage panel, and an Alt + Tab style printer switcher

The dashboard mixed setup buttons with a plain list of printers, and switching printers jumped straight to the next one with no view of where you were going. The dashboard is now a grid of status tiles (state in large type, progress bar, file, route, state-colored edge, a fleet summary line), and Discover, Add, Network, Gamepad, Update and the saved-printer editor moved behind a **Manage** button. Switching now shows a panel of the same tiles over any screen: hold Ctrl and press Tab to step, release Ctrl to switch, Escape to cancel; gamepad next/previous step through and switch after a 1.5 s pause; tapping a tile switches immediately.

Status polling moved out of the dashboard into a window-wide monitor so the switcher has fresh data while a printer's controls are open. It keeps the existing schedule: 15 s for LAN printers, 60 s through OctoEverywhere.

Validation: 91 tests pass on Linux. The dashboard, Manage panel and switcher were rendered with GTK 3.24 under WSLg against stubbed KlipperScreen panels, and a harness drove the real window class through Ctrl + Tab and release, Shift + Ctrl + Tab and Escape, gamepad next with the timed commit, tapping a tile, another action closing the switcher, and focus loss cancelling it. That run caught the switcher popup sizing itself wider than the screen from long file names, fixed here. Not yet run on the Pi or the Waveshare panel, and the popup's centering over the real KlipperScreen window is untested.

### Changes

- jogger/status.py: new; `fetch_status`, `StatusMonitor` (per-printer schedules, removal of deleted printers), `fleet_summary`, `headline`, `route_text`.
- jogger/ui.py: `PrinterTile`; new tile `Dashboard`; new `Manage` panel (old dashboard controls and saved-printer list); `Switcher` popup; tile labels capped so the popup fits the screen.
- jogger/integration.py: window-wide status monitor; switcher open/step/commit/cancel for keyboard (commit on Ctrl release), gamepad (timed commit) and touch; `kdj_manage` panel registered.
- jogger/style.css: tile, state color, progress bar and switcher styles.
- README.md, docs/dashboard.png, docs/switcher.png: new dashboard, Manage, switching and keyboard table.
- tests/test_status.py: new.

## 2026-09-27: Full updates from the touchscreen with passwordless sudo

The Update button could not run the installer because the touchscreen cannot answer a sudo password prompt, so installer changes still needed SSH. Raspberry Pi OS gives the first user passwordless sudo by default, and it works on the farm controller. When `sudo -n true` succeeds, the button now fast-forwards the checkout and runs `scripts/install.sh` as the app user in a transient systemd unit (`kdj-update`). The unit sits outside the app's own service, so the installer's final `systemctl restart` of the app cannot kill it halfway. `install.sh` is run directly instead of `update.sh`, because `update.sh` pulls a new copy of itself while bash is still reading it. If the unit stops and the app is still running, the screen reports that the installer did not finish and points to `journalctl -u kdj-update`. Without passwordless sudo, the limited user-owned update is unchanged.

Validation: 80 tests pass on Linux; sudo and systemd calls are recorded, not executed, in tests. Not yet run on the Pi.

### Changes

- jogger/updater.py: `can_sudo`, `start_system_update`, `system_update_running`; shared local-change refusal and fast-forward.
- jogger/ui.py: Update panel uses the full update when available, watches the `kdj-update` unit, and reports an installer that stopped without restarting the app.
- README.md: full and limited update modes.
- tests/test_updater.py: full update, local-change refusal, sudo check and unit state tests.

## 2026-09-27: Update button on the dashboard

Updating needed an SSH session to run `scripts/update.sh`. The dashboard now has an **Update** button that checks GitHub, lists what is new, and installs it with a restart of the app.

The installer needs sudo, which the touchscreen cannot prompt for, so the in-app update only does the user-owned steps: fast-forward the checkout, move the pinned KlipperScreen checkout when `klipperscreen.ref` changes, and reinstall `requirements.txt` into the app venv when it changes. If an update touches `scripts/install.sh`, the screen tells the user to finish with `scripts/update.sh` over SSH. Checkouts with local edits or local commits are never updated from the touchscreen.

Validation: updater tests run against real temporary git repositories (76 tests pass on Linux). The update panel itself has not been run on the Pi yet.

### Changes

- jogger/updater.py: new; `check` (fetch, updates available, local edits or commits, installer changes) and `apply` (fast-forward, KlipperScreen ref, pip) with timeouts and no prompts.
- jogger/ui.py: **Update** button on the dashboard and a new Update panel with confirmation.
- jogger/integration.py: registers the panel; exposes the source and KlipperScreen directories.
- README.md: touchscreen and SSH update instructions.
- tests/test_updater.py: new.

## 2026-09-27: Printer names from Moonraker, OctoEverywhere without an App ID, automatic route switching

Printers now name themselves, OctoEverywhere remote access can be linked without an OctoEverywhere App ID and without typing a URL on the touchscreen, and the controller moves between the LAN and OctoEverywhere on its own in both directions.

Why:

- **Names never filled in.** Name detection read `hostname` from Moonraker's `/server/info`, which does not contain one (it is in Klipper's `/printer/info`). Discovery and Test connection now use the Mainsail or Fluidd printer name stored in Moonraker's database, then Klipper's host name.
- **OctoEverywhere setup needed an App ID we do not have**, and ran the portal in a browser embedded in the touchscreen. That browser could not use KlipperScreen's on-screen keyboard to sign in. It was also torn down from inside its own navigation callback, and upstream can re-run a panel's constructor while the old browser is still loading, both likely causes of the app falling over. Setup now happens on a phone: the touchscreen shows a QR code for a short-lived page served by the controller, where a Shared Connection URL is pasted (or, with an App ID, the portal returns to that page).
- **Switching was one-way and slow.** The controller only failed over after an initialized session dropped, never moved back to the LAN, and probed the LAN with a 350 ms connect timeout that Wi-Fi often misses. Endpoints are now probed in parallel with a wall-clock deadline, a numeric LAN fallback address is kept for when `.local` stops resolving, and a remote session returns to the LAN when it becomes reachable.

Validation: automated tests pass (71 on Linux). The GTK panels could not be run in this environment, and nothing here has been validated on the Pi, a real printer or a real OctoEverywhere account yet.

### Changes

- jogger/network.py: `Client.details()` reads Mainsail/Fluidd names, Klipper host name and the OctoEverywhere printer ID; `Client.summary()` for dashboard cards; OctoEverywhere 6xx codes mapped to readable errors; `select_endpoint` probes LAN, LAN fallback and OctoEverywhere in parallel with deadlines and reports a `problem`; new `local_endpoint`, `verify_remote` (same-printer check), `lan_fallback_url`, `clean_name`; `prefer_hostname_url` takes the host name and only replaces numeric addresses.
- jogger/octoeverywhere.py: Shared Connection parsing (embedded or separate basic-auth credentials, rejects website pages, non-HTTPS and query strings); `error_message` for documented 6xx codes; `portal_url` accepts `returnUrl`; stored links record `kind`.
- jogger/handoff.py: new one-time local setup page for the phone (private clients only, unguessable path, expiry, size and bad-request caps, no-referrer and no-store).
- jogger/ui.py: live status on dashboard cards; discovery marks saved printers and opens the editor with an automatic test; the editor fills the printer's name until you type one and offers it otherwise; remote access links from a phone via QR code; App ID and the legacy remote option moved under Advanced; the embedded WebKit browser is gone; fixed discovery results being treated as existing profiles (renaming could delete a saved printer with the discovered name, and "Remove connection" was shown for unsaved results).
- jogger/integration.py: switch back to the LAN from OctoEverywhere every 30 s when safe; route popups; failover also covers the LAN fallback address; endpoint problems (e.g. OctoEverywhere 605) shown when connecting.
- scripts/install.sh, requirements.txt, .github/workflows/tests.yml: WebKitGTK no longer installed; `segno` added for the QR code.
- README.md, docs/VALIDATION.md: printer names, dashboard status, OctoEverywhere linking and switching behavior, validation status.
- tests: new test_handoff.py; name, selection, 6xx, verification and Shared Connection tests.
