#!/usr/bin/env python3
"""Reopen interactive Claude Code sessions in gnome-terminal tabs after a reboot.

A timer records the live sessions to one snapshot file per boot. At login,
`auto-restore` reopens the sessions from the most recent *previous* boot, so
the new boot's (initially empty) snapshot can never hide the list we need.

`restore` (also the "Claude Restore" app launcher) shows a checkbox list of the
most recent sessions that are not running, however they ended.

~/.claude/sessions/<pid>.json and the transcript format are not documented
interfaces. If Claude Code changes them, `list` and `restore` will stop showing
sessions.
"""
import argparse
import glob
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
STATE_DIR = Path(
    os.environ.get("CLAUDE_RESTORE_STATE", Path.home() / ".local/state/claude-restore")
)
SNAPSHOTS = STATE_DIR / "snapshots"
SYSTEMD_DIR = Path.home() / ".config/systemd/user"
AUTOSTART = Path.home() / ".config/autostart/claude-restore.desktop"
LAUNCHER = Path.home() / ".local/share/applications/claude-restore.desktop"
TIMER = "claude-restore-snapshot.timer"
SERVICE = "claude-restore-snapshot.service"

# During shutdown sessions die one by one, and a snapshot taken then would
# record only the survivors. Sessions stay listed this long after they vanish.
GRACE_SECONDS = 150
KEEP_SNAPSHOTS = 10
PICKER_LIMIT = 15


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def proc_start(pid):
    """Return the kernel start time of pid, or None if it is not running."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces, so split after its closing paren.
    return stat.rsplit(")", 1)[1].split()[19]


def live_sessions():
    """Return interactive CLI sessions whose process is really still running."""
    sessions = []
    for path in (CLAUDE_DIR / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text())
            if data.get("kind") != "interactive" or data.get("entrypoint") != "cli":
                continue
            if proc_start(data["pid"]) != str(data["procStart"]):
                continue  # stale file left behind by a crash, or a reused pid
            sessions.append(
                {
                    "sessionId": data["sessionId"],
                    "cwd": data["cwd"],
                    "name": data.get("name"),
                    "pid": data["pid"],
                    "startedAt": data.get("startedAt", 0),
                }
            )
        except (OSError, ValueError, KeyError):
            continue
    return sessions


def has_transcript(session_id):
    """A session with no messages yet has nothing that --resume could open."""
    return bool(glob.glob(str(CLAUDE_DIR / "projects" / "*" / f"{session_id}.jsonl")))


def load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def snapshot():
    now = time.time()
    boot = boot_id()
    path = SNAPSHOTS / f"{boot}.json"
    previous = (load(path) or {}).get("sessions", [])
    live = live_sessions()
    live_ids = {s["sessionId"] for s in live}
    live_pids = {s["pid"] for s in live}

    sessions = [dict(s, lastSeen=now) for s in live]
    for old in previous:
        # A live pid under a new id means /clear or /resume replaced the old one.
        if old["sessionId"] in live_ids or old["pid"] in live_pids:
            continue
        if now - old["lastSeen"] < GRACE_SECONDS:
            sessions.append(old)

    save(path, {"bootId": boot, "updatedAt": now, "sessions": sessions})
    for stale in sorted(SNAPSHOTS.glob("*.json"), key=os.path.getmtime)[:-KEEP_SNAPSHOTS]:
        stale.unlink()
    return sessions


def previous_snapshot():
    """Return (path, data) for the newest snapshot not from this boot."""
    current = f"{boot_id()}.json"
    candidates = [p for p in SNAPSHOTS.glob("*.json") if p.name != current]
    for path in sorted(candidates, key=os.path.getmtime, reverse=True):
        data = load(path)
        if data is not None:
            return path, data
    return None, None


def claude_binary():
    found = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    return os.environ.get("CLAUDE_RESTORE_BIN", found)


def terminal_command(sessions):
    """Build one gnome-terminal call that opens every session in a new window.

    It must be one call: a separate `--tab` call attaches to whichever window
    gnome-terminal chooses, which can be one the user already has open. That
    is why this uses the deprecated per-tab -e instead of `--`, which can only
    give a command to a single terminal.
    """
    argv = ["gnome-terminal"]
    for index, session in enumerate(sessions):
        # bash -i loads ~/.bashrc, where API keys and PATH are often set. The
        # trailing shell keeps the tab open if claude exits or fails to resume.
        script = (
            f"{shlex.quote(claude_binary())} --resume {shlex.quote(session['sessionId'])}; "
            "exec bash"
        )
        argv += [
            "--tab" if index else "--window",
            f"--working-directory={session['cwd']}",
            "-e",
            shlex.join(["bash", "-ic", script]),
        ]
    return argv


def log(message):
    print(message)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(STATE_DIR / "restore.log", "a") as handle:
        handle.write(f"{time.strftime('%F %T')} {message}\n")


def restore(dry_run=False, force=False, delay=0):
    path, data = previous_snapshot()
    if data is None:
        log("No snapshot from a previous boot.")
        return 0
    if data.get("restoredAt") and not force:
        log("Previous boot's sessions were already restored (use --force to redo).")
        return 0

    if delay and not dry_run:
        time.sleep(delay)

    live_ids = {s["sessionId"] for s in live_sessions()}
    todo = []
    for session in sorted(data["sessions"], key=lambda s: s.get("startedAt", 0)):
        label = f"{session['sessionId']} ({session.get('name')}) in {session['cwd']}"
        if session["sessionId"] in live_ids:
            log(f"skip, already running: {label}")
        elif not os.path.isdir(session["cwd"]):
            log(f"skip, directory is gone: {label}")
        elif not has_transcript(session["sessionId"]):
            log(f"skip, no conversation saved: {label}")
        else:
            todo.append((session, label))

    if dry_run:
        for _, label in todo:
            print(f"would restore: {label}")
        return 0

    # Mark first: a repeat run must not open a second set of tabs.
    data["restoredAt"] = time.time()
    save(path, data)

    if not todo:
        log("Nothing to restore.")
        return 0
    try:
        subprocess.run(terminal_command([s for s, _ in todo]), check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        log(f"FAILED to open terminal: {error}")
        return 1
    for _, label in todo:
        log(f"restored: {label}")
    return 0


def describe_transcript(path):
    """Return a session dict for an interactive CLI transcript, else None."""
    session = None
    titles = {}
    try:
        with open(path, errors="replace") as handle:
            for line in handle:
                # Transcripts reach many MB, so parse only the lines we need.
                if '"aiTitle"' in line or '"agentName"' in line:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    titles.update({k: entry[k] for k in ("aiTitle", "agentName") if k in entry})
                elif session is None and '"cwd"' in line:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") != "user" or entry.get("isSidechain"):
                        continue
                    if entry.get("entrypoint") != "cli":
                        return None
                    session = {"sessionId": path.stem, "cwd": entry["cwd"]}
        if session is None:
            return None
        session["lastActive"] = path.stat().st_mtime
    except (OSError, KeyError):
        return None
    session["title"] = titles.get("aiTitle") or titles.get("agentName") or "(untitled)"
    return session


def recent_sessions(limit=PICKER_LIMIT):
    """Return the newest resumable sessions that are not running now."""
    live_ids = {s["sessionId"] for s in live_sessions()}
    paths = [Path(p) for p in glob.glob(str(CLAUDE_DIR / "projects" / "*" / "*.jsonl"))]
    found = []
    for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stem in live_ids:
            continue
        session = describe_transcript(path)
        if session and os.path.isdir(session["cwd"]):
            found.append(session)
            if len(found) == limit:
                break
    return found


PICKER_HINT = "↑ ↓ move    Space tick    Enter restore    Esc cancel"


def load_gtk():
    """Import GTK 3 only when a dialog is shown, so the rest works headless."""
    import gi

    # GTK 4 may be installed too, so pin both, or Gdk can load as 4.0.
    gi.require_version("Gdk", "3.0")
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, Gtk, Pango

    if not Gtk.init_check(sys.argv)[0]:
        raise RuntimeError("cannot open the display")
    return Gdk, Gtk, Pango


def build_picker(rows):
    """Return (dialog, tree, store) for rows of (when, title, folder, id)."""
    Gdk, Gtk, Pango = load_gtk()
    dialog = Gtk.Dialog(title="Claude Restore")
    dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Restore", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.OK)
    dialog.set_default_size(1000, 560)
    dialog.set_position(Gtk.WindowPosition.CENTER)

    store = Gtk.ListStore(bool, str, str, str, str)
    for row in rows:
        store.append([False, *row])
    tree = Gtk.TreeView(model=store)
    tree.set_enable_search(False)  # typing must not open a search box over the list

    def flip(path):
        row = store.get_iter(path)
        store.set_value(row, 0, not store.get_value(row, 0))

    toggle = Gtk.CellRendererToggle()
    toggle.connect("toggled", lambda _renderer, path: flip(path))
    tree.append_column(Gtk.TreeViewColumn("", toggle, active=0))
    for index, heading in enumerate(("Last active", "Session", "Folder"), start=1):
        text = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn(heading, text, text=index)
        column.set_resizable(True)
        column.set_expand(index == 2)
        tree.append_column(column)

    def on_key(_tree, event):
        if event.keyval == Gdk.KEY_space:
            path, _ = tree.get_cursor()
            if path is not None:
                flip(path)
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            dialog.response(Gtk.ResponseType.OK)
            return True
        return False

    tree.connect("key-press-event", on_key)
    scroller = Gtk.ScrolledWindow(vexpand=True)
    scroller.add(tree)
    box = dialog.get_content_area()
    box.set_spacing(8)
    box.set_border_width(10)
    box.pack_start(Gtk.Label(label=PICKER_HINT, xalign=0), False, False, 0)
    box.pack_start(scroller, True, True, 0)
    dialog.show_all()
    if rows:
        tree.set_cursor(Gtk.TreePath(0))
    tree.grab_focus()
    return dialog, tree, store


def picked_ids(tree, store):
    """The ticked ids, or the highlighted row's id if nothing is ticked."""
    ticked = [row[4] for row in store if row[0]]
    if ticked:
        return ticked
    path, _ = tree.get_cursor()
    return [store[path][4]] if path is not None else []


def run_picker(rows):
    """Show the dialog; return the chosen ids, or [] when cancelled."""
    _, Gtk, _ = load_gtk()
    dialog, tree, store = build_picker(rows)
    chosen = picked_ids(tree, store) if dialog.run() == Gtk.ResponseType.OK else []
    dialog.destroy()
    return chosen


def show_message(text):
    _, Gtk, _ = load_gtk()
    dialog = Gtk.MessageDialog(text=text, buttons=Gtk.ButtonsType.OK, title="Claude Restore")
    dialog.run()
    dialog.destroy()


def choose_sessions(sessions):
    """Ask which sessions to reopen; an empty list means none or cancelled."""
    rows = []
    for s in sessions:
        when = time.strftime("%a %d %b %H:%M", time.localtime(s["lastActive"]))
        rows.append((when, " ".join(s["title"].split()), s["cwd"], s["sessionId"]))
    chosen = set(run_picker(rows))
    return [s for s in sessions if s["sessionId"] in chosen]


def pick():
    """Show the checkbox list and reopen whatever the user ticks."""
    try:
        sessions = recent_sessions()
        if not sessions:
            show_message("No sessions to restore.")
            return 0
        chosen = choose_sessions(sessions)
    except (ImportError, ValueError, RuntimeError) as error:
        log(f"FAILED to show the picker: {error}")
        return 1
    if not chosen:
        return 0
    try:
        subprocess.run(terminal_command(chosen), check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        log(f"FAILED to open terminal: {error}")
        return 1
    for s in chosen:
        log(f"restored: {s['sessionId']} ({s['title']}) in {s['cwd']}")
    return 0


def show():
    live = live_sessions()
    print(f"Running now: {len(live)}")
    for s in live:
        print(f"  {s['sessionId']}  {s.get('name')}  {s['cwd']}")
    _, data = previous_snapshot()
    if data is None:
        print("Previous boot: no snapshot")
        return
    restored = "already restored" if data.get("restoredAt") else "not restored yet"
    print(f"Previous boot: {len(data['sessions'])} session(s), {restored}")
    for s in data["sessions"]:
        print(f"  {s['sessionId']}  {s.get('name')}  {s['cwd']}")


def install():
    script = Path(__file__).resolve()
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    (SYSTEMD_DIR / SERVICE).write_text(
        "[Unit]\nDescription=Record running Claude Code sessions\n\n"
        f"[Service]\nType=oneshot\nExecStart={sys.executable} \"{script}\" snapshot\n"
    )
    (SYSTEMD_DIR / TIMER).write_text(
        "[Unit]\nDescription=Record running Claude Code sessions every minute\n\n"
        "[Timer]\nOnActiveSec=10s\nOnUnitActiveSec=60s\nAccuracySec=5s\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(
        "[Desktop Entry]\nType=Application\nName=Restore Claude Code sessions\n"
        f"Exec={sys.executable} \"{script}\" auto-restore --delay 8\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    LAUNCHER.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER.write_text(
        "[Desktop Entry]\nType=Application\nName=Claude Restore\n"
        "Comment=Reopen recent Claude Code sessions\n"
        f"Exec={sys.executable} \"{script}\" restore\n"
        "Icon=utilities-terminal\nTerminal=false\nCategories=Utility;\n"
        "Keywords=claude;restore;session;resume;\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", TIMER], check=True)
    print(f"Installed {TIMER}, {AUTOSTART} and {LAUNCHER}")


def uninstall():
    subprocess.run(["systemctl", "--user", "disable", "--now", TIMER], check=False)
    for path in (SYSTEMD_DIR / TIMER, SYSTEMD_DIR / SERVICE, AUTOSTART, LAUNCHER):
        path.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    print(f"Removed. Snapshots are still in {STATE_DIR}")


def main():
    parser = argparse.ArgumentParser(description="Reopen Claude Code sessions after a reboot.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("snapshot", help="record the running sessions")
    commands.add_parser("list", help="show running and previous-boot sessions")
    commands.add_parser("install", help="enable the timer and login restore")
    commands.add_parser("uninstall", help="remove the timer and login restore")
    commands.add_parser("restore", help="choose recent sessions to reopen")
    restore_parser = commands.add_parser("auto-restore", help="reopen previous-boot sessions")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--force", action="store_true", help="restore again")
    restore_parser.add_argument("--delay", type=float, default=0, help="seconds to wait first")
    args = parser.parse_args()

    if args.command == "snapshot":
        snapshot()
    elif args.command == "list":
        show()
    elif args.command == "install":
        install()
    elif args.command == "uninstall":
        uninstall()
    elif args.command == "restore":
        return pick()
    else:
        return restore(args.dry_run, args.force, args.delay)
    return 0


if __name__ == "__main__":
    sys.exit(main())
