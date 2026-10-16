# OBS Stream Guard

A small Windows tool that stops you from leaving an OBS stream live
by mistake.

- **You always see that you are live.** A red `LIVE h:mm:ss` bar and
  a red outline sit on the monitor you stream. OBS does not record
  them. A small LIVE pill sits in the corner of every other monitor,
  so you also see it over Parsec or Remote Desktop.
- **Hard stop at a set time.** A stream that started before 16:00
  (local time) stops at 16:00. The bar flashes yellow 5 minutes before.
- **Hard cap.** Any stream longer than 9 hours stops.
- **Fail-safe.** If OBS does not stop in 30 seconds, the guard kills
  `obs64.exe`. If OBS runs but the guard cannot reach it, a grey
  "NOT CONNECTED" marker tells you to check the stream yourself.

One Python file, standard library only.

## Requirements

- Windows 10 2004 or later (for hide-from-capture)
- Python 3.8+ with Tk (the python.org installer has it)
- OBS 28+ with WebSocket on: Tools > WebSocket Server Settings >
  Enable WebSocket server (port 4455)

The guard reads the WebSocket password from the OBS config at run
time. You do not type it anywhere.

## Use

Try it (shows the markers for 60 seconds, no stream needed):

```
pythonw stream_guard.pyw --demo
```

Other checks:

```
python stream_guard.pyw --selftest
python stream_guard.pyw --status
```

Run it at every logon with a scheduled task:

```
schtasks /Create /TN "OBS Stream Guard" /SC ONLOGON /RL LIMITED ^
  /TR "pythonw.exe \"C:\path\to\stream_guard.pyw\""
```

Only one copy runs at a time. It logs to `stream_guard.log` next to
the script.

## Settings

Edit the constants at the top of `stream_guard.pyw`:

| Name         | Default | Meaning                                 |
|--------------|---------|-----------------------------------------|
| `CUTOFF`     | 16:00   | Stop time, local clock                  |
| `WARN_MIN`   | 5       | Yellow warning, minutes before cutoff   |
| `MAX_HOURS`  | 9       | Longest allowed stream                  |
| `STOP_GRACE_S` | 30    | Seconds before OBS is force-killed      |
| `PILL_INSET` | 24      | Pill distance from the screen corner    |

## How it works

- Polls OBS `GetStreamStatus` every 3 seconds over obs-websocket v5.
- Finds the monitor that your Display Capture source records.
- Hides the bar and outline from capture with
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`.
- The pill is click-through, so it never blocks your work.

## Known limitations

- Windows only.
- If your remote app shows only the streamed monitor, you see no
  marker there (it is hidden from all screen capture).
- A stream that starts after the cutoff time runs until the 9-hour cap.

## License

MIT. See `LICENSE`.
