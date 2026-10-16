"""OBS stream guard.

- While OBS is streaming: red "LIVE h:mm:ss" bar + red outline on the streamed monitor
  (hidden from OBS), and a small LIVE pill in the corner of every other monitor
  (visible over Parsec / Remote Desktop).
- Grey marker if OBS runs but the guard cannot reach it (guard is blind).
- Hard stop: stream that started before CUTOFF is stopped at CUTOFF (local time = ET).
- Hard cap: any stream longer than MAX_HOURS is stopped.
- If StopStream does not work in STOP_GRACE_S, obs64.exe is killed.
Stdlib only. Password is read from the OBS websocket config at run time.
"""
import base64, ctypes, hashlib, json, os, socket, struct, subprocess, sys, time
from ctypes import wintypes
import tkinter as tk
from datetime import datetime, time as dtime, timedelta

CUTOFF = dtime(16, 0)        # market close, local time (PC is on Eastern)
WARN_MIN = 5                 # bar turns yellow this many minutes before CUTOFF
MAX_HOURS = 9                # cap for any stream (evenings, weekends)
STOP_GRACE_S = 30
POLL_MS = 3000
FAIL_TICKS = 4  # failed checks in a row (~20 s) before the grey "not connected" warning
PORT = 4455
CFG = os.path.expandvars(r"%APPDATA%\obs-studio\plugin_config\obs-websocket\config.json")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream_guard.log")
LOCK_PORT = 47455            # single-instance lock
EDGE_PX = 6                  # outline width, streamed monitor (hidden from OBS)
PILL_ALPHA = 0.85            # other monitors: LIVE pill opacity
PILL_INSET = 24              # other monitors: pill distance from the bottom-right corner of the work area
KEY = "#010203"              # see-through color of the outline window


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


# ---------- Windows helpers: capture monitor, hide-from-capture, click-through ----------
user32 = ctypes.windll.user32
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
SCENES = os.path.expandvars(r"%APPDATA%\obs-studio\basic\scenes")


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def obs_monitor_ids():
    ids = []
    for name in os.listdir(SCENES):
        if name.endswith(".json"):
            try:
                with open(os.path.join(SCENES, name), encoding="utf-8") as f:
                    for s in json.load(f).get("sources", []):
                        if s.get("id") == "monitor_capture" and s.get("settings", {}).get("monitor_id"):
                            ids.append(s["settings"]["monitor_id"].lower())
            except Exception:
                pass
    return ids


def monitors():
    """[(device, x, y, w, h, primary)] in physical pixels."""
    out = []

    def cb(hmon, hdc, rect, data):
        mi = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        wk = mi.rcWork  # monitor minus taskbar
        out.append((mi.szDevice, r.left, r.top, r.right - r.left, r.bottom - r.top, bool(mi.dwFlags & 1),
                    (wk.right, wk.bottom)))
        return True
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors(None, None, proto(cb), 0)
    return out


def capture_monitor():
    """Monitor that OBS Display Capture records; primary if not found."""
    ids = obs_monitor_ids()
    dev = None
    i = 0
    while ids and dev is None:
        ad = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(ad), 0):
            break
        j = 0
        md = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
        while user32.EnumDisplayDevicesW(ad.DeviceName, j, ctypes.byref(md), 1):  # 1 = interface name
            if md.DeviceID.lower() in ids:
                dev = ad.DeviceName
            j += 1
        i += 1
    mons = monitors()
    return next((m for m in mons if m[0] == dev), None) or next(m for m in mons if m[5])


def hwnd_of(win):
    return user32.GetParent(win.winfo_id())


def set_window(win, hidden, click_through=False):
    hwnd = hwnd_of(win)
    if hidden and not user32.SetWindowDisplayAffinity(hwnd, 0x11):  # WDA_EXCLUDEFROMCAPTURE
        log(f"SetWindowDisplayAffinity failed: {ctypes.GetLastError()}")
    if click_through:
        ex = user32.GetWindowLongPtrW(hwnd, -20)  # GWL_EXSTYLE
        user32.SetWindowLongPtrW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x8000000)
        # layered | transparent | toolwindow | noactivate


# ---------- minimal websocket client (text frames, localhost only) ----------
def _send(sock, text):
    data = text.encode()
    mask = os.urandom(4)
    n = len(data)
    head = bytes([0x81])
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _recv(sock):
    msg = b""
    while True:
        b1, b2 = _exact(sock, 2)
        op, n = b1 & 0x0F, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _exact(sock, 8))[0]
        mask = _exact(sock, 4) if b2 & 0x80 else None
        payload = _exact(sock, n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if op == 8:
            raise ConnectionError(f"server closed: {payload[2:].decode(errors='replace')}")
        if op in (9, 10):
            continue  # ping/pong: ignore, connection is short-lived
        msg += payload
        if b1 & 0x80:
            return json.loads(msg)


class OBS:
    """One short connection per call. Raises on any failure."""

    def __init__(self):
        with open(CFG, encoding="utf-8") as f:
            self.password = json.load(f).get("server_password", "")

    def call(self, request_type, data=None):
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=3)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{PORT}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                          f"Sec-WebSocket-Protocol: obswebsocket.json\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(1024)
                if not chunk:
                    raise ConnectionError("handshake closed")
                resp += chunk
            if b" 101 " not in resp.split(b"\r\n")[0]:
                raise ConnectionError(resp.split(b"\r\n")[0].decode())
            hello = _recv(sock)["d"]
            ident = {"rpcVersion": 1, "eventSubscriptions": 0}
            a = hello.get("authentication")
            if a:
                secret = base64.b64encode(hashlib.sha256((self.password + a["salt"]).encode()).digest())
                ident["authentication"] = base64.b64encode(
                    hashlib.sha256(secret + a["challenge"].encode()).digest()).decode()
            _send(sock, json.dumps({"op": 1, "d": ident}))
            if _recv(sock)["op"] != 2:
                raise ConnectionError("identify failed")
            _send(sock, json.dumps({"op": 6, "d": {"requestType": request_type, "requestId": "g",
                                                   "requestData": data or {}}}))
            while True:
                m = _recv(sock)
                if m["op"] == 7:
                    d = m["d"]
                    if not d["requestStatus"]["result"]:
                        raise RuntimeError(f"{request_type}: {d['requestStatus']}")
                    return d.get("responseData", {})
        finally:
            sock.close()


def obs_running():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"],
                         capture_output=True, text=True, creationflags=0x08000000).stdout
    return "obs64.exe" in out


def must_stop(now, live_s):
    """Return reason string if the stream must stop, else None."""
    started = now - timedelta(seconds=live_s)
    cutoff_today = datetime.combine(now.date(), CUTOFF)
    if started < cutoff_today <= now:
        return "market close"
    if live_s >= MAX_HOURS * 3600:
        return f"over {MAX_HOURS}h"
    return None


# ---------------------------------- UI --------------------------------------
class Overlay:
    """Live marker on one monitor.
    streamed=True: draggable LIVE bar + red outline, hidden from OBS and other screen capture.
    streamed=False: small see-through, click-through LIVE pill in the bottom-right corner, visible to
    Parsec / Remote Desktop (they capture the screen the same way OBS does)."""

    def __init__(self, root, mon, streamed):
        _, x, y, w, h, _, self.corner = mon
        self.streamed = streamed
        self.bar = tk.Toplevel(root)
        self.bar.overrideredirect(True)
        if streamed:
            self.label = tk.Label(self.bar, font=("Segoe UI", 13, "bold"), fg="white", padx=14, pady=4)
        else:
            self.label = tk.Label(self.bar, font=("Segoe UI", 8, "bold"), fg="white", padx=5, pady=0)
        self.label.pack()
        wins = [self.bar]
        if streamed:
            self.label.bind("<ButtonPress-1>", self._grab)
            self.label.bind("<B1-Motion>", self._drag)
            self.edge = tk.Toplevel(root)
            self.edge.overrideredirect(True)
            self.edge.config(bg=KEY)
            self.edge.attributes("-transparentcolor", KEY)
            c = tk.Canvas(self.edge, bg=KEY, highlightthickness=0)
            c.pack(fill="both", expand=True)
            t = EDGE_PX
            c.create_rectangle(t // 2, t // 2, w - t // 2 - 1, h - t // 2 - 1, outline="#ff0000", width=t)
            wins.append(self.edge)
        else:
            self.bar.attributes("-alpha", PILL_ALPHA)
        for win in wins:  # map off-screen first so the HWND exists
            win.attributes("-topmost", True)
            win.geometry("10x10+-20000+-20000")
            win.update()
        set_window(self.bar, streamed, click_through=not streamed)
        if streamed:
            set_window(self.edge, True, click_through=True)
            self.edge.geometry(f"{w}x{h}+{x}+{y}")
            self.edge.update()
        self.label.config(text="\u25CF LIVE 0:00:00")  # size the window for the usual text
        self.bar.geometry("")
        self.bar.update_idletasks()
        if streamed:
            self.bar.geometry(f"+{x + (w - self.bar.winfo_reqwidth()) // 2}+{y + EDGE_PX}")
        else:
            self._to_corner()
        self.bar.update()  # apply the move now; a pending move was lost on one monitor

    def _grab(self, e):
        self._dx, self._dy = e.x, e.y

    def _drag(self, e):
        self.bar.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _to_corner(self):
        self.bar.update_idletasks()
        r, b = self.corner
        self.bar.geometry(f"+{r - PILL_INSET - self.bar.winfo_reqwidth()}+{b - PILL_INSET - self.bar.winfo_reqheight()}")

    def show(self, text, bg, edge):
        changed = text != self.label.cget("text")
        self.label.config(text=text, bg=bg)
        self.bar.config(bg=bg)
        if changed and not self.streamed:
            self._to_corner()  # keep the right edge in the corner when the text width changes
        self.bar.attributes("-topmost", True)  # re-assert over full-screen apps
        self.bar.lift()
        if self.streamed:
            if edge:
                self.edge.deiconify()
                self.edge.attributes("-topmost", True)
            else:
                self.edge.withdraw()

    def destroy(self):
        self.bar.destroy()
        if self.streamed:
            self.edge.destroy()

class Bar:
    def __init__(self):
        self.obs = OBS()
        self.stop_sent_at = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlays = []
        self.flash = False
        self.fails = 0
        self.tick()

    def show(self, text, bg, edge=True):
        if not self.overlays:  # built once per stream, so a dragged bar stays put
            cap = capture_monitor()[0]
            self.overlays = [Overlay(self.root, m, streamed=(m[0] == cap)) for m in monitors()]
        for o in self.overlays:
            o.show(text, bg, edge)

    def hide(self):
        for o in self.overlays:
            o.destroy()
        self.overlays = []
    def tick(self):
        try:
            self.check()
        except Exception as e:  # never let the guard die
            log(f"tick error: {e!r}")
        self.root.after(POLL_MS, self.tick)

    def check(self):
        now = datetime.now()
        try:
            st = self.obs.call("GetStreamStatus")
        except Exception as e:
            self.fails += 1
            if self.fails == FAIL_TICKS:
                log(f"not connected: {e!r}")
            # OBS refuses or says "not ready" for a few seconds while it opens or closes; only warn if it lasts
            if obs_running() and self.fails >= FAIL_TICKS:
                self.show("OBS GUARD NOT CONNECTED - check stream yourself", "#555555", edge=False)
                if self.stop_sent_at:
                    self.escalate(now)
            elif not obs_running():
                self.stop_sent_at = None
                self.hide()
            return
        self.fails = 0

        if not st.get("outputActive"):
            if self.stop_sent_at:
                log("stream stopped")
            self.stop_sent_at = None
            self.hide()
            return

        live_s = st.get("outputDuration", 0) / 1000
        h, rem = divmod(int(live_s), 3600)
        clock = f"{h}:{rem // 60:02d}:{rem % 60:02d}"
        reason = must_stop(now, live_s)
        if reason:
            if not self.stop_sent_at:
                log(f"StopStream ({reason}), live {clock}")
                self.stop_sent_at = now
                try:
                    self.obs.call("StopStream")
                except Exception as e:
                    log(f"StopStream failed: {e!r}")
            self.show(f"STOPPING STREAM ({reason})", "#000000")
            self.escalate(now)
            return

        to_cut = datetime.combine(now.date(), CUTOFF) - now
        if timedelta(0) < to_cut <= timedelta(minutes=WARN_MIN):
            self.flash = not self.flash
            mins = int(to_cut.total_seconds() // 60) + 1
            self.show(f"● LIVE {clock}  -  STOPS IN {mins} MIN", "#d4a000" if self.flash else "#cc0000")
        else:
            self.show(f"● LIVE {clock}", "#cc0000")

    def escalate(self, now):
        if self.stop_sent_at and (now - self.stop_sent_at).total_seconds() > STOP_GRACE_S:
            log("StopStream did not work in time: killing obs64.exe")
            subprocess.run(["taskkill", "/IM", "obs64.exe", "/F"], capture_output=True, creationflags=0x08000000)
            self.stop_sent_at = None


def selftest():
    d = datetime(2026, 10, 16)
    at = lambda hh, mm: d.replace(hour=hh, minute=mm)
    assert must_stop(at(15, 59), 3600) is None
    assert must_stop(at(16, 0), 3600) == "market close"
    assert must_stop(at(16, 1), 3600) == "market close"          # missed tick still stops
    assert must_stop(at(23, 0), 17 * 3600) == "market close"     # forgotten stream, 17h later
    assert must_stop(at(19, 0), 3600) is None                    # evening stream allowed
    assert must_stop(at(23, 30), 9 * 3600) == "market close"     # started 14:30
    assert must_stop(at(3, 0), 9 * 3600) == f"over {MAX_HOURS}h"  # overnight cap
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit()
    if "--status" in sys.argv:
        print(OBS().call("GetStreamStatus"))
        sys.exit()
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels, so the outline fits the monitor
    if "--demo" in sys.argv:  # show bar + outline for 60 s without a stream
        Bar.check = lambda self: self.show("● LIVE 0:00:00  (TEST)", "#cc0000")
        b = Bar()
        b.root.after(60000, b.root.destroy)
        b.root.mainloop()
        sys.exit()
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        sys.exit()  # already running
    log("guard started")
    Bar().root.mainloop()
