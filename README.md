# Discord Message Auto

Watch a Discord text channel during a daily time window and submit one configured
message as soon as its message editor becomes usable. The app uses Playwright
and a dedicated Chromium browser profile that you log into manually.

**Being first is not guaranteed.** This app reacts to the opening as displayed in
its browser; Discord delivery, network latency, other participants and the time
needed to prepare the message determine its final position.

Discord prohibits automating personal accounts, including browser-based
self-bots, and may terminate an account for doing so. See
[Discord's official policy](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots).

## Requirements

- Python 3.11 or newer (tested locally with Python 3.14).
- Windows for the included double-click launchers; the Python CLI also supports
  macOS and Linux with Playwright's browser dependencies installed.
- Internet access and a Discord account with access to the target text channel.
- A computer that stays awake throughout the monitoring window.

## Quick start on Windows

1. Clone or download this repository, then run `installer.cmd`. It creates
   `.venv`, installs the Python dependencies and downloads Chromium.
2. Copy `config.example.json` to `config.json` and edit the channel URL and message.
   Alternatively, run `configurer.cmd` to use the setup wizard.
3. Run `connexion.cmd`. Sign into Discord in the dedicated browser using a QR
   code or your credentials and 2FA. Press Enter in the terminal when finished.
4. Run `verifier-maintenant.cmd` for a preview. It waits for the editor without
   typing, sending, or updating the daily send state. Stop with Ctrl+C if needed.
5. Run `demarrer.cmd` and leave the program running. Stop it with Ctrl+C.

For an existing local installation, keep your own `config.json` and browser
profile. They are deliberately excluded from Git. Restart the running watcher
when changing the code or configuration: settings are loaded once at startup.

## Configuration

Example `config.json`:

```json
{
  "channel_url": "https://discord.com/channels/123456789012345678/123456789012345679",
  "message": "Check-in ✅",
  "opening_time": "20:00",
  "timezone": "Indian/Antananarivo",
  "minutes_before": 5,
  "minutes_after": 15,
  "check_interval_seconds": 2
}
```

Right-click the target text channel in Discord and choose **Copy Link**. Use its
full `https://discord.com/channels/SERVER_ID/CHANNEL_ID` URL. The example IDs above
are placeholders; replace them with your own. Check the exact message required
by your server before starting.

| Setting | Meaning | Accepted values |
| --- | --- | --- |
| `channel_url` | Exact server text-channel URL; a trailing slash is accepted. | Two positive numeric IDs. |
| `message` | Text to send; emoji and `\n` line breaks are supported. | Nonempty, at most 2,000 characters. |
| `opening_time` | Expected opening time in the configured timezone. | `HH:MM`, default `20:00`. |
| `timezone` | IANA timezone used for scheduling and the daily send key. | Default `Indian/Antananarivo`. |
| `minutes_before` | Start watching this many minutes before opening. | 0–180, default 5. |
| `minutes_after` | Keep waiting this many minutes after opening. | 1–180, default 15. |
| `check_interval_seconds` | Main-loop recovery interval, not a delay imposed after an opening. | 1–60, default 2. |

The defaults watch from **19:55 until 20:15 in Madagascar**. The opening time of
20:00 corresponds to 19:00 in Switzerland during summer time and 18:00 during
winter time. Timezone conversion is automatic. Increase `minutes_after` if the
channel owner often opens late.

`opening_time` defines a monitoring window, not a scheduled keypress. If the
channel is already writable at 19:55, the message is sent then. If the owner opens
at 20:05, the app reacts then. If the editor stays unavailable through 20:15, that
day is skipped. There is no send outside the window in scheduled mode.

## How it works and what is optimized

1. The scheduler calculates the active or next daily window. Outside that window,
   the Python process stays alive without keeping a browser open.
2. At the start of the window, the browser opens the exact configured channel and
   warms up Playwright's input helpers.
3. A browser-side `MutationObserver` watches for an editable, visible message
   composer. DOM changes wake the watcher immediately; a 100 ms browser-side
   fallback also catches visibility changes caused by CSS. The configured main
   interval is used for recovery and checking the monitoring deadline.
4. The observer returns the composer snapshot and existing message IDs together,
   avoiding an extra browser round trip immediately after opening. A single-line
   message is filled in one operation; multiline messages use Shift+Enter.
5. The app checks the channel, focus and prepared draft, writes a daily reservation
   to disk, and presses Enter. Existing drafts are preserved and stop the attempt.
6. A second observer looks for a new matching message for up to ten seconds. This
   avoids the previous 500 ms confirmation polling delay. The app records the
   result, closes the browser and waits for the next day.

Chromium background timer/rendering throttling is disabled for this dedicated
browser. A normally loaded, closed channel is not periodically reloaded; recovery
reloads are limited to pages with no main application container. This avoids
interrupting the live interface around the expected opening.

`check-in.log` reports preparation through Enter and the time until a matching
message is observed, in milliseconds. These measurements exclude browser startup
and the wait for opening. They do not measure the owner's permission change,
network round-trip time, server acceptance, or your rank among participants.

## Duplicate protection and confirmation

Only one automatic attempt is allowed per channel and scheduled day. `state.json`
is written **before** Enter. Both `reserved` and `confirmed` records block another
attempt after a restart. A project lock also prevents simultaneous instances.

A crash between reservation and Enter can cause a missed message. An uncertain
send is never retried automatically. Before manually removing a day's entry from
`state.json`, stop the program and check Discord for the message. Clearing state
can allow a duplicate. Changing the message does not reset the reservation.

Confirmation is a new visible message row with matching normalized text. It is a
UI observation, not a server acknowledgement: an optimistic message or another
participant posting identical text can satisfy it. It does not prove that a scrim
bot accepted your check-in. Follow your server's rules and check its receipt.

## Commands

Run from the project directory after installation:

```powershell
.\.venv\Scripts\python.exe main.py --setup
.\.venv\Scripts\python.exe main.py --login
.\.venv\Scripts\python.exe main.py --check
.\.venv\Scripts\python.exe main.py --now --preview
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe main.py --now
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

`--check` validates your configuration without opening a browser. `--now --preview`
starts a non-sending preview for `minutes_after` minutes. **`--now` without
`--preview` can send immediately**, using today's date in the configured timezone
and still respecting the daily reservation.

On macOS/Linux, create a virtual environment with `python3 -m venv .venv`, install
with `.venv/bin/python -m pip install -r requirements.txt`, then run
`.venv/bin/python -m playwright install --with-deps chromium`. Copy the example
configuration and use `.venv/bin/python main.py` with the same flags.

## Troubleshooting

- **Login required:** stop the watcher and run `connexion.cmd` again. Complete any
  2FA or CAPTCHA manually.
- **Another instance is active:** stop the existing watcher. Deleting the lock
  file does not stop a running process.
- **Existing draft / preparation failed:** inspect the dedicated browser's draft
  and channel. Clear it manually only if you no longer need it.
- **No message:** check the URL, permissions, configured timezone, daily state and
  `check-in.log`. Slowmode, moderation or lost permissions can reject a message
  even if the editor appears editable.
- **Browser install error:** rerun `installer.cmd` and check Python installation
  and internet connectivity.
- **Interface changed:** Discord UI changes may require updating `COMPOSER` or
  `MESSAGE_ROWS` in `main.py`. Threads and custom check-in forms are unsupported.

Keep the computer awake and avoid interacting with the dedicated Discord window
while the watcher is armed. Mentions use Discord's normal behavior.

## Local files and repository contents

- `main.py`: browser session, opening detection, message preparation and CLI.
- `schedule.py`: configuration validation, daily windows, persistent state and lock.
- `tests/`: isolated browser and scheduler regression tests.
- `.github/workflows/tests.yml`: GitHub Actions tests on Python 3.11 and 3.14.
- `config.example.json`: shareable configuration example.
- Windows `.cmd` launchers and `installer.ps1`: installation and routine commands.

`config.json`, `browser-profile/`, `state.json`, logs, the lock and `.venv/` stay
local. **The browser profile contains your authenticated session: never upload
it.** The terminal does not request Discord passwords or tokens. The tests use
the example configuration and intercept all browser requests with a local fake
channel; they neither contact Discord nor send real messages. CI is therefore
independent of your personal configuration and login.
