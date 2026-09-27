# Changelog

## 2026-09-27: Readable confirmation prompts

Confirmation prompts (install update, remove connection, and the gamepad's resume, cancel print, home, heaters off and macro confirmations) showed white text on a white box. They used a stock Gtk.MessageDialog, which draws Adwaita's light dialog background while KlipperScreen's stylesheet forces white text on every widget. They now use KlipperScreen's own dialog (the one its panels use for confirmations): dark, full screen, with large Yes and No buttons. The prompt no longer blocks the GTK main loop while open, and the gamepad lock it sets is released even when KlipperScreen closes open dialogs on a panel change without an answer.

Validation: the GTK harness loads KlipperScreen's real KlippyGtk and theme, renders the old dialog (mean background 171,171,171 with white text) and the new one (23,29,32), and checks that Yes runs the action, No does not, and a dialog destroyed without an answer leaves the gamepad unlocked. The connection editor, gamepad setup and update screens were also rendered under the theme and are readable. Not yet run on the Pi.

### Changes

- jogger/integration.py: `kdj_confirm` uses KlipperScreen's `gtk.Dialog` with Yes/No and releases its modal lock on answer or destroy.

## 2026-09-27: Ctrl + Tab steps through every printer; number keys in the switcher

Holding Ctrl and pressing Tab only ever reached the next printer. With no window manager, X keyboard focus can move onto the switcher popup once it appears, so the main window stops being active. Later Tab presses arrived through the popup and were forwarded correctly, but the action handler dropped any input while the main window was inactive, so releasing Ctrl always switched to the first printer highlighted. Input is now accepted while the switcher is open. Switcher tiles are also numbered, and pressing 1 to 9 jumps straight to that printer.

Validation: the GTK harness now makes the main window inactive once the popup opens and sends the following Tabs through the popup, as happens on the Pi. That check fails on the previous code (stuck on the first printer) and passes now: five Tabs forward and one back land on the fifth printer, and 8 and keypad 9 switch directly. All other harness checks pass at 1280x800 and 1024x600. Not yet run on the Pi.

### Changes

- jogger/integration.py: switcher input accepted while its popup holds focus; number keys pick a printer.
- jogger/ui.py: numbered switcher tiles; updated hint.
- README.md, docs/switcher.png: stepping, number keys.

## 2026-09-27: SSH terminal missing after updating; dashboard scrolled on the Pi

**"Terminal support is not installed yet."** The previous update added a system package (the VTE terminal widget) through the installer. The update on the Pi was most likely performed by an older installed version whose Update button could not run the installer, so the package never arrived; the earlier instructions wrongly said tapping Update would run the full installer. Once the code was current, Update only said "up to date" and there was no way to finish from the touchscreen. The installer now writes a SHA-256 stamp of itself when it completes, and the Update screen compares it with the current installer: if they differ it shows **System setup needs to finish** with a **Finish setup** button that runs the full installer. The SSH panel's missing-terminal message links straight there. Installs from before this change have no stamp, so the first check after updating offers to finish setup once.

**Tiny scroll on the dashboard.** The previous layout was tested with GTK's default ~15 px font, but KlipperScreen sets every widget's font from the screen size (about 30 px on 1280x800, 22 px on 1024x600) and gives buttons a margin, so tiles came out taller than the grid assumed. The dashboard now measures a tile's real minimum height (with and without the route and file lines) before choosing columns, removes the theme margin from tiles, sizes tile text relative to KlipperScreen's font, keeps the summary on one line, and, if a row still overflows, lays out again with that much less height. The switcher popup was also wider than a 1024 px screen at these font sizes; its columns now depend on the screen width and it is capped to the screen.

Validation: 153 tests pass on Linux. The harness now loads KlipperScreen's own base.css and z-bolt theme at the font size KlipperScreen computes for each panel, and confirmed nine printers fully visible at 1280x800 and 1024x600 (and in areas 40 px smaller each way), the switcher within the screen at both sizes, and all switching, Ctrl + R and SSH checks. Not yet run on the Pi.

### Changes

- scripts/install.sh: writes `setup-stamp` (SHA-256 of itself) to the app data directory on completion.
- jogger/updater.py: `setup_current`.
- jogger/ui.py: Update screen offers **Finish setup** when setup is behind; SSH panel links to it; tiles measure their minimum heights and drop the route line when short; dashboard overflow safety net; one-line summary; switcher columns by screen width and capped width.
- jogger/layout.py: grid takes measured minimum and maximum tile heights.
- jogger/style.css: tile text relative to KlipperScreen's font; no theme margin on tiles; smaller switcher text; dashboard hint style.
- README.md, docs/dashboard.png, docs/switcher.png: finishing setup, dashboard fitting.
- tests/test_updater.py, tests/test_layout.py: stamp and measured-height tests.

## 2026-09-27: Switcher stays open while Ctrl is held; nine printers on screen; Ctrl + R; SSH terminal

**Ctrl + Tab popup only flashed.** On the Pi the switcher appeared and vanished at once. The app runs under X with no window manager, where keyboard focus can follow the pointer onto the new popup; the main window then got a focus-out event, which the switcher treated as "cancel", and the Ctrl release could go to the popup instead of the main window. The cause on the Pi is inferred from this, not observed. The switcher now reads the physical Ctrl key every 50 ms while it is open and switches when Ctrl is let go, ignores focus-out, never accepts focus itself, and forwards any keys it does receive. If the Ctrl state cannot be read, the key-release event still works.

**Nine printers without scrolling.** The dashboard grid is fitted to the space KlipperScreen leaves it: columns and tile height are chosen from the real size so nine printers fit on 1280x800 and 1024x600 panels, with short tiles dropping the file-name line first. Rendering at the 1024x600 size caught GTK theme padding around each tile pushing the last row out of view; the app stylesheet now removes it.

**Ctrl + R** on the dashboard re-checks every printer at once. A refresh asked for while one is running now runs straight after it, instead of being overwritten by the one in flight (a bug found by the new test).

**SSH.** A dashboard button opens an embedded terminal (VTE) that asks for the host (defaulting to the current printer's LAN address), the user name, and then hands over to ssh for the password. Host and user are passed as arguments and checked, so input cannot inject ssh options or shell commands. Leaving the panel ends the session.

Validation: unit tests pass on Linux, including the real prompt script against a stand-in ssh. The GTK harness (GTK 3.24 and VTE 2.91 under WSLg, stubbed KlipperScreen) confirmed nine tiles fully visible at 1180x740 and 924x540, the switcher staying open through a focus-out while Ctrl is held and switching on release, the release-event fallback, Ctrl + R re-fetching all printers, terminal key passthrough, and the real ssh starting with the chosen host and user name (it stopped at the host lookup, since the test host does not exist, so the password prompt itself was not reached). Not yet run on the Pi; the focus behavior in particular needs confirming there.

### Changes

- jogger/integration.py: Ctrl polling for the switcher; focus-out no longer cancels; Ctrl + R on the dashboard; terminal key passthrough; `kdj_ssh` panel registered.
- jogger/ui.py: switcher popup does not take focus and forwards keys; dashboard grid fitted to its area; tiles resize and hide the file line when short; SSH button and `SshPanel`.
- jogger/layout.py: new; grid sizing.
- jogger/terminal.py: new; SSH prompt script and LAN host choice.
- jogger/status.py: queued refresh; forced refreshes survive a refresh in flight.
- jogger/style.css: no theme padding around dashboard tiles.
- scripts/install.sh: installs `gir1.2-vte-2.91` and `openssh-client`, checks VTE imports.
- README.md, docs/dashboard.png, docs/switcher.png, docs/ssh.png: dashboard fitting, Ctrl + R, switcher behavior, SSH terminal.
- tests/test_layout.py, tests/test_terminal.py: new; tests/test_status.py: refresh during a refresh.

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
