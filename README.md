# claude-restore

Bring your [Claude Code](https://claude.com/claude-code) terminal sessions back on Linux, after a reboot, a crash, or an accidental quit.

- **Pick from a list:** press Super, type `restore`, tick the sessions you want, and they reopen as tabs in one new terminal window.
- **Automatic after a reboot:** sessions that were open when the machine went down reopen at login.

It is a single Python file with no third-party dependencies.

## Requirements

- Linux with a GNOME-style desktop and `gnome-terminal`
- `zenity` for the checkbox dialog
- systemd user services
- Python 3.8 or newer
- Claude Code, with `claude` on your `PATH` or at `~/.local/bin/claude`

Developed and tested on Pop!_OS (GNOME, X11). On Debian or Ubuntu, the desktop packages can be installed with:

```bash
sudo apt install gnome-terminal zenity
```

## Install

```bash
git clone https://github.com/alexcpn/claude-restore.git
cd claude-restore
python3 claude_restore.py install
```

`install` records the absolute path of the script, so if you move the folder, run it again.

It creates three things, all in your home directory:

| File | Purpose |
| --- | --- |
| `~/.config/systemd/user/claude-restore-snapshot.{service,timer}` | Records running sessions every minute |
| `~/.config/autostart/claude-restore.desktop` | Reopens the previous boot's sessions at login |
| `~/.local/share/applications/claude-restore.desktop` | Adds "Claude Restore" to the app launcher |

If the app does not appear when you search for it, log out and back in.

## Usage

Press **Super**, type **restore**, and press Enter. The list shows the 10 most recent sessions that are not running, with their last-active time, title and folder.

From a terminal:

| Command | What it does |
| --- | --- |
| `python3 claude_restore.py restore` | Opens the checkbox picker |
| `python3 claude_restore.py list` | Shows running sessions and the previous boot's sessions |
| `python3 claude_restore.py auto-restore --dry-run` | Shows what the login step would reopen |
| `python3 claude_restore.py auto-restore --force` | Reopens the previous boot's sessions again |

The picker leaves out headless (`sdk-cli`) runs, sessions that are already running, and sessions whose folder no longer exists.

After a reboot, auto-restore reopens each boot's sessions only once. A session has to be open for a timer run, about a minute, and have at least one message to be included. Sessions you quit more than about 150 seconds before the shutdown are not reopened automatically, but you can still get them from the picker.

## Uninstall

```bash
python3 claude_restore.py uninstall
rm -rf ~/.local/state/claude-restore
```

`uninstall` removes the timer and both launcher entries. The second line removes the saved snapshots and log.

## Limitations

This tool reads `~/.claude/sessions/<pid>.json` and the transcripts in `~/.claude/projects/`. Neither is a documented interface, so a Claude Code update can stop sessions from being listed.

Only `gnome-terminal` is supported.

## Privacy and security

- The tool makes no network connections.
- It reads session metadata and transcript files locally, and shows only each session's title, folder and last-active time.
- Snapshots in `~/.local/state/claude-restore/` contain session IDs, names and folder paths, but no conversation content. The `snapshots` folder is created with mode `0700`.
- Terminal commands are built with `shlex` quoting, and no value from a transcript is run unquoted.

CI runs on every push and pull request, and weekly:

| Scan | Covers |
| --- | --- |
| [Bandit](https://bandit.readthedocs.io/) | Python security issues; fails on Medium severity or higher |
| [Gitleaks](https://github.com/gitleaks/gitleaks) | Secrets anywhere in git history |
| [CodeQL](https://codeql.github.com/) | Static analysis; runs only while the repository is public, because GitHub requires a paid plan for private repositories |

Dependabot keeps the pinned GitHub Actions up to date.

## Development

```bash
python3 -m unittest -v
```

The tests use temporary directories and mock the terminal and dialog, so they do not touch your real sessions.

## License

[Apache License 2.0](LICENSE)
