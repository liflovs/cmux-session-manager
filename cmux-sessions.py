#!/usr/bin/env python3
"""
Snapshot and restore cmux workspaces with Claude Code sessions.

Captures the full cmux workspace layout (from cmux's session file) and
cross-references running Claude processes to map session IDs. On restore,
recreates workspaces and resumes Claude sessions.

Usage:
  cmux-sessions snapshot                  # Save current state (all workspaces)
  cmux-sessions snapshot -w myproject     # Snapshot only matching workspace
  cmux-sessions snapshot -o state.json    # Save to specific file
  cmux-sessions list                      # Show active Claude sessions
  cmux-sessions diff                      # Compare snapshot vs live workspaces
  cmux-sessions restore                   # Restore from latest snapshot
  cmux-sessions restore -w myproject      # Restore only matching workspace
  cmux-sessions restore -f state.json     # Restore from specific file
  cmux-sessions restore --skip-active     # Restore only closed workspaces
  cmux-sessions restore --dry-run         # Preview what would be restored
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CMUX_SESSION_FILE = os.path.expanduser(
    "~/Library/Application Support/cmux/session-com.cmuxterm.app.json"
)
CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SNAPSHOT_DIR = os.path.expanduser("~/.cmux-snapshots")


# ── Process discovery ────────────────────────────────────────


def get_claude_processes():
    """Get all running Claude processes with their session IDs and working directories."""
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,command"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return []

    processes = []
    for line in result.stdout.splitlines():
        if "claude" not in line.lower() or "grep" in line:
            continue

        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue

        pid = parts[0]
        cmd = parts[1]

        # Extract session ID from --session-id or --resume flags
        session_id = None
        for flag in ("--session-id", "--resume"):
            match = re.search(rf"{flag}\s+(\S+)", cmd)
            if match:
                session_id = match.group(1)
                break

        if not session_id:
            continue

        # Get working directory via lsof
        cwd = get_process_cwd(pid)

        processes.append({
            "pid": int(pid),
            "session_id": session_id,
            "cwd": cwd,
        })

    return processes


def get_process_cwd(pid):
    """Get the working directory of a process."""
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid)],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 9 and parts[3] == "cwd":
                return parts[-1]
    except Exception:
        pass
    return None


def get_git_branch(directory):
    """Get the current git branch for a directory, walking up to 5 parent levels."""
    d = directory
    for _ in range(6):
        head = os.path.join(d, ".git", "HEAD")
        if os.path.isfile(head):
            try:
                with open(head) as f:
                    content = f.read().strip()
                if content.startswith("ref: refs/heads/"):
                    return content[16:]
                return content[:12]  # detached HEAD — show short hash
            except Exception:
                return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


SHELL_NAMES = {"bash", "zsh", "fish", "login", "sshd", "sh"}
NOISE_CMDS = {"sleep", "ps", "head", "tail", "read", "cat", "grep", "awk", "sed"}


def get_terminal_commands():
    """Discover foreground commands running in terminal shells.

    Returns a dict: cwd -> [command_string, ...] for non-Claude, non-shell processes
    whose parent is a shell.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,comm"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        return {}

    # Build parent lookup
    children = {}  # ppid -> [(pid, comm)]
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, comm = parts[0], parts[1], parts[2]
        children.setdefault(ppid, []).append((pid, comm))

    # Find shell processes and their foreground children
    commands_by_cwd = {}
    for line in result.stdout.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        shell_pid, _, comm = parts[0], parts[1], parts[2]

        # Only look at shells
        base_comm = os.path.basename(comm).lstrip("-")
        if base_comm not in SHELL_NAMES:
            continue

        # Check children of this shell
        for child_pid, child_comm in children.get(shell_pid, []):
            base_child = os.path.basename(child_comm)
            # Skip noise and other shells
            if base_child in NOISE_CMDS or base_child in SHELL_NAMES:
                continue
            # Skip Claude processes (handled separately)
            if "claude" in child_comm.lower():
                continue

            # Get the full command line
            try:
                args_result = subprocess.run(
                    ["ps", "-p", child_pid, "-o", "args="],
                    capture_output=True, text=True, timeout=3
                )
                full_cmd = args_result.stdout.strip()
            except Exception:
                full_cmd = child_comm

            if not full_cmd:
                continue

            # Get parent shell's cwd
            cwd = get_process_cwd(shell_pid)
            if cwd:
                commands_by_cwd.setdefault(cwd, []).append(full_cmd)

    return commands_by_cwd


# ── Claude session index ─────────────────────────────────────


def get_claude_session_info(project_path, session_id):
    """Look up Claude session metadata from the sessions index."""
    encoded = project_path.replace("/", "-")
    index_path = os.path.join(CLAUDE_PROJECTS_DIR, encoded, "sessions-index.json")

    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                data = json.load(f)
            for entry in data.get("entries", []):
                if entry.get("sessionId") == session_id:
                    return {
                        "summary": entry.get("summary", ""),
                        "firstPrompt": entry.get("firstPrompt", ""),
                        "modified": entry.get("modified", ""),
                        "messageCount": entry.get("messageCount", 0),
                        "gitBranch": entry.get("gitBranch", ""),
                    }
        except Exception:
            pass

    # Fallback: check if the session .jsonl file exists directly
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, encoded)
    jsonl_path = os.path.join(project_dir, f"{session_id}.jsonl")
    if os.path.exists(jsonl_path):
        try:
            mtime = os.path.getmtime(jsonl_path)
            modified = datetime.fromtimestamp(mtime).isoformat()
            return {
                "summary": "",
                "firstPrompt": "",
                "modified": modified,
                "messageCount": 0,
                "gitBranch": "",
                "note": "from-file",
            }
        except Exception:
            pass

    return None


def find_latest_claude_session(project_path):
    """Find the most recent Claude session for a given project path."""
    encoded = project_path.replace("/", "-")
    index_path = os.path.join(CLAUDE_PROJECTS_DIR, encoded, "sessions-index.json")

    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                data = json.load(f)
            entries = data.get("entries", [])
            if entries:
                entries.sort(key=lambda e: e.get("modified", ""), reverse=True)
                return entries[0]
        except Exception:
            pass

    # Fallback: scan .jsonl files by modification time
    project_dir = os.path.join(CLAUDE_PROJECTS_DIR, encoded)
    if not os.path.isdir(project_dir):
        return None

    try:
        jsonl_files = sorted(
            Path(project_dir).glob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if jsonl_files:
            newest = jsonl_files[0]
            mtime = newest.stat().st_mtime
            return {
                "sessionId": newest.stem,
                "summary": "",
                "modified": datetime.fromtimestamp(mtime).isoformat(),
                "messageCount": 0,
                "gitBranch": "",
                "note": "from-file",
            }
    except Exception:
        pass

    return None


# ── cmux state ───────────────────────────────────────────────


def load_cmux_session():
    """Load the cmux session state file."""
    if not os.path.exists(CMUX_SESSION_FILE):
        print(f"Error: cmux session file not found at {CMUX_SESSION_FILE}", file=sys.stderr)
        sys.exit(1)

    with open(CMUX_SESSION_FILE) as f:
        return json.load(f)


def parse_layout(layout):
    """Recursively parse a cmux layout tree into a serializable description."""
    layout_type = layout.get("type")
    if layout_type == "pane":
        pane = layout.get("pane", {})
        return {
            "type": "pane",
            "panelIds": pane.get("panelIds", []),
            "selectedPanelId": pane.get("selectedPanelId"),
        }
    elif layout_type == "split":
        split = layout.get("split", {})
        return {
            "type": "split",
            "orientation": split.get("orientation", "vertical"),
            "dividerPosition": split.get("dividerPosition", 0.5),
            "first": parse_layout(split.get("first", {})),
            "second": parse_layout(split.get("second", {})),
        }
    return {"type": "unknown"}


def collect_layout_pane_ids(layout):
    """Walk a parsed layout tree and return panel IDs in left-to-right / top-to-bottom order."""
    if layout.get("type") == "pane":
        return layout.get("panelIds", [])
    elif layout.get("type") == "split":
        return collect_layout_pane_ids(layout.get("first", {})) + collect_layout_pane_ids(layout.get("second", {}))
    return []


def layout_to_splits(layout):
    """Convert a layout tree into a sequence of split operations.

    Returns a list of dicts:
      {"panelIds": [...], "direction": "right"|"down", "position": float}
    The first entry is the initial pane (no split needed).
    """
    result = []
    _walk_layout(layout, result, is_root=True)
    return result


def _walk_layout(layout, result, is_root=False):
    if layout.get("type") == "pane":
        result.append({
            "panelIds": layout.get("panelIds", []),
            "direction": None,  # filled in by parent split
        })
    elif layout.get("type") == "split":
        orientation = layout.get("orientation", "vertical")
        direction = "right" if orientation == "vertical" else "down"

        _walk_layout(layout.get("first", {}), result, is_root=False)
        # Mark the next entry as needing a split
        start = len(result)
        _walk_layout(layout.get("second", {}), result, is_root=False)
        if start < len(result):
            result[start]["direction"] = direction


# ── Commands ─────────────────────────────────────────────────


def _match_workspace(ws, ws_idx, filter_name):
    """Check if a workspace matches the given filter (title or index)."""
    if filter_name is None:
        return True
    # Match by index
    if filter_name.isdigit() and int(filter_name) == ws_idx:
        return True
    # Match by title (case-insensitive substring)
    cwd = ws.get("currentDirectory", "")
    title = ws.get("customTitle") or ws.get("title") or (os.path.basename(cwd) if cwd else f"workspace-{ws_idx}")
    return filter_name.lower() in title.lower()


def cmd_snapshot(args):
    """Capture current cmux + Claude state."""
    cmux_data = load_cmux_session()
    claude_procs = get_claude_processes()
    terminal_cmds = get_terminal_commands()

    # Build a lookup: cwd -> [claude sessions], consumed as matched
    claude_by_cwd = {}
    for proc in claude_procs:
        cwd = proc.get("cwd")
        if cwd:
            claude_by_cwd.setdefault(cwd, []).append(proc)

    ws_filter = getattr(args, "workspace", None)

    snapshot_data = {
        "version": 2,
        "timestamp": datetime.now().isoformat(),
        "windows": [],
    }

    matched_any = False

    for win_idx, win in enumerate(cmux_data.get("windows", [])):
        window = {"index": win_idx, "workspaces": []}

        tm = win.get("tabManager", {})
        selected_idx = tm.get("selectedWorkspaceIndex", 0)

        for ws_idx, ws in enumerate(tm.get("workspaces", [])):
            if not _match_workspace(ws, ws_idx, ws_filter):
                continue
            matched_any = True
            cwd = ws.get("currentDirectory", "")
            title = ws.get("customTitle") or ws.get("title") or (os.path.basename(cwd) if cwd else f"workspace-{ws_idx}")
            layout = parse_layout(ws.get("layout", {}))

            workspace = {
                "index": ws_idx,
                "title": title,
                "cwd": cwd,
                "isSelected": ws_idx == selected_idx,
                "isPinned": ws.get("isPinned", False),
                "layout": layout,
                "panels": [],
            }

            # Build panel lookup by ID for layout ordering
            panels_by_id = {p.get("id"): p for p in ws.get("panels", [])}

            for panel in ws.get("panels", []):
                panel_id = panel.get("id", "")
                panel_dir = panel.get("directory", cwd)
                panel_title = panel.get("title", "")
                panel_type = panel.get("type", "terminal")
                is_pinned = panel.get("isPinned", False)
                terminal = panel.get("terminal", {})
                terminal_cwd = terminal.get("workingDirectory", panel_dir)

                # Determine if this is a Claude panel
                is_claude = "Claude" in panel_title or panel_title.startswith("\u2733")
                claude_session = None

                if is_claude and terminal_cwd:
                    # Try to match with a running Claude process (consume match)
                    matches = claude_by_cwd.get(terminal_cwd, [])
                    if matches:
                        proc = matches.pop(0)
                        claude_session = {
                            "session_id": proc["session_id"],
                            "pid": proc["pid"],
                        }
                        meta = get_claude_session_info(terminal_cwd, proc["session_id"])
                        if meta:
                            claude_session["summary"] = meta.get("summary", "")
                            claude_session["gitBranch"] = meta.get("gitBranch", "")
                    else:
                        # No running process — find latest session from index
                        latest = find_latest_claude_session(terminal_cwd)
                        if latest:
                            claude_session = {
                                "session_id": latest["sessionId"],
                                "pid": None,
                                "summary": latest.get("summary", ""),
                                "gitBranch": latest.get("gitBranch", ""),
                                "note": "from-index",
                            }

                # Capture running command for non-Claude terminal panels
                last_command = None
                if not is_claude and terminal_cwd:
                    cmds = terminal_cmds.get(terminal_cwd, [])
                    if cmds:
                        last_command = cmds.pop(0)  # consume to avoid duplicates

                panel_data = {
                    "id": panel_id,
                    "title": panel_title,
                    "type": panel_type,
                    "directory": terminal_cwd,
                    "isPinned": is_pinned,
                    "isClaude": is_claude,
                }
                if claude_session:
                    panel_data["claudeSession"] = claude_session
                if last_command:
                    panel_data["lastCommand"] = last_command

                workspace["panels"].append(panel_data)

            window["workspaces"].append(workspace)

        if window["workspaces"]:
            snapshot_data["windows"].append(window)

    if ws_filter and not matched_any:
        print(f"Error: No workspace matching '{ws_filter}' found.", file=sys.stderr)
        sys.exit(1)

    # Save snapshot
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    if args.output:
        out_path = args.output
    elif getattr(args, "name", None):
        out_path = os.path.join(SNAPSHOT_DIR, f"cmux-{args.name}.json")
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(SNAPSHOT_DIR, f"cmux-{ts}.json")

    with open(out_path, "w") as f:
        json.dump(snapshot_data, f, indent=2)

    # Also symlink as "latest"
    latest_path = os.path.join(SNAPSHOT_DIR, "latest.json")
    with open(latest_path, "w") as f:
        json.dump(snapshot_data, f, indent=2)

    # Summary
    total_ws = sum(len(w["workspaces"]) for w in snapshot_data["windows"])
    total_panels = sum(
        len(ws["panels"])
        for w in snapshot_data["windows"]
        for ws in w["workspaces"]
    )
    total_claude = sum(
        1 for w in snapshot_data["windows"]
        for ws in w["workspaces"]
        for p in ws["panels"] if p.get("isClaude")
    )
    claude_with_session = sum(
        1 for w in snapshot_data["windows"]
        for ws in w["workspaces"]
        for p in ws["panels"] if p.get("claudeSession")
    )

    print(f"Snapshot saved: {out_path}")
    if ws_filter:
        print(f"  Filter:           '{ws_filter}'")
    print(f"  Windows:          {len(snapshot_data['windows'])}")
    print(f"  Workspaces:       {total_ws}")
    print(f"  Panels:           {total_panels}")
    print(f"  Claude panels:    {total_claude} ({claude_with_session} with session IDs)")


def cmd_list(args):
    """List active Claude sessions across all cmux workspaces."""
    cmux_data = load_cmux_session()
    claude_procs = get_claude_processes()

    # Build process lookup by cwd — consume matches to avoid duplicates
    claude_by_cwd = {}
    for proc in claude_procs:
        cwd = proc.get("cwd")
        if cwd:
            claude_by_cwd.setdefault(cwd, []).append(proc)

    rows = []
    for win in cmux_data.get("windows", []):
        for ws in win.get("tabManager", {}).get("workspaces", []):
            ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
            if len(ws_title) > 25:
                ws_title = ws_title[:22] + "..."

            all_panels = ws.get("panels", [])
            total_panels = len(all_panels)
            claude_count = sum(
                1 for p in all_panels
                if "Claude" in p.get("title", "") or p.get("title", "").startswith("\u2733")
            )
            non_claude_count = total_panels - claude_count
            panels_str = f"{total_panels} ({claude_count}C/{non_claude_count}T)"

            for panel in all_panels:
                panel_title = panel.get("title", "")
                is_claude = "Claude" in panel_title or panel_title.startswith("\u2733")
                if not is_claude:
                    continue

                panel_dir = panel.get("terminal", {}).get(
                    "workingDirectory", panel.get("directory", "")
                )

                # Consume matching process to avoid double-counting
                session_id = "-"
                status = "stopped"
                matches = claude_by_cwd.get(panel_dir, [])
                if matches:
                    proc = matches.pop(0)
                    sid = proc["session_id"]
                    session_id = sid[:12] + "..." if len(sid) > 15 else sid
                    status = "running"

                short_dir = panel_dir.replace(os.path.expanduser("~"), "~")
                if len(short_dir) > 45:
                    short_dir = "..." + short_dir[-42:]

                clean_title = panel_title.replace("\u2733 ", "").replace("\u2733", "")
                if len(clean_title) > 35:
                    clean_title = clean_title[:32] + "..."

                # Git branch — try session metadata first, fall back to .git/HEAD
                branch = "-"
                if panel_dir:
                    meta = None
                    if matches:
                        # Already consumed above, but we can look up metadata
                        meta = get_claude_session_info(panel_dir, proc["session_id"]) if status == "running" else None
                    if meta and meta.get("gitBranch"):
                        branch = meta["gitBranch"]
                    else:
                        branch = get_git_branch(panel_dir) or "-"
                    if len(branch) > 20:
                        branch = branch[:17] + "..."

                rows.append((ws_title, panels_str, clean_title, short_dir, branch, session_id, status))

    if not rows:
        print("No Claude sessions found in cmux workspaces.")
        return

    headers = ("WORKSPACE", "PANELS", "CLAUDE SESSION", "DIRECTORY", "BRANCH", "SESSION ID", "STATUS")
    widths = [max(len(h), max(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)

    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))

    running = sum(1 for r in rows if r[6] == "running")
    print(f"\n{len(rows)} Claude panels, {running} running")


def cmd_show(args):
    """Show detailed info for a workspace (active or from a snapshot)."""
    home = os.path.expanduser("~")

    if args.file:
        # Show from snapshot
        snap_path = args.file
        if not os.path.exists(snap_path):
            print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
            sys.exit(1)

        with open(snap_path) as f:
            snap = json.load(f)

        found = False
        for win in snap.get("windows", []):
            for ws in win.get("workspaces", []):
                title = ws.get("title", "untitled")
                if args.workspace and args.workspace.lower() not in title.lower():
                    continue
                found = True
                _show_snapshot_workspace(ws, snap_path, snap.get("timestamp", "unknown"), home)

        if not found:
            print(f"Error: No workspace matching '{args.workspace}' in snapshot.", file=sys.stderr)
            sys.exit(1)
    else:
        # Show from live cmux session
        cmux_data = load_cmux_session()
        claude_procs = get_claude_processes()
        terminal_cmds = get_terminal_commands()

        claude_by_cwd = {}
        for proc in claude_procs:
            cwd = proc.get("cwd")
            if cwd:
                claude_by_cwd.setdefault(cwd, []).append(proc)

        found = False
        for win in cmux_data.get("windows", []):
            for ws in win.get("tabManager", {}).get("workspaces", []):
                ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
                if args.workspace and args.workspace.lower() not in ws_title.lower():
                    continue
                found = True
                _show_live_workspace(ws, claude_by_cwd, terminal_cmds, home)

        if not found:
            if args.workspace:
                print(f"Error: No workspace matching '{args.workspace}' found.", file=sys.stderr)
            else:
                print("No workspaces found.", file=sys.stderr)
            sys.exit(1)


def _show_live_workspace(ws, claude_by_cwd, terminal_cmds, home):
    """Print detailed info for a live workspace."""
    ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
    ws_cwd = ws.get("currentDirectory", "")
    panels = ws.get("panels", [])

    print(f"Workspace: {ws_title}")
    print(f"Directory: {ws_cwd.replace(home, '~')}")
    print(f"Panels:    {len(panels)}")
    print()

    for i, panel in enumerate(panels):
        panel_title = panel.get("title", "")
        is_claude = "Claude" in panel_title or panel_title.startswith("\u2733")
        terminal = panel.get("terminal", {})
        panel_cwd = terminal.get("workingDirectory", panel.get("directory", ""))
        short_cwd = panel_cwd.replace(home, "~")

        kind = "claude" if is_claude else "terminal"
        clean_title = panel_title.replace("\u2733 ", "").replace("\u2733", "").strip()
        if not clean_title:
            clean_title = "(untitled)"

        print(f"  Panel {i + 1}: [{kind}] {clean_title}")
        print(f"    cwd: {short_cwd}")

        if is_claude:
            # Try to match running process
            matches = claude_by_cwd.get(panel_cwd, [])
            if matches:
                proc = matches.pop(0)
                print(f"    session: {proc['session_id']}")
                print(f"    pid: {proc['pid']}")
                print(f"    status: running")
                meta = get_claude_session_info(panel_cwd, proc["session_id"])
                if meta:
                    if meta.get("summary"):
                        print(f"    summary: {meta['summary']}")
                    if meta.get("gitBranch"):
                        print(f"    branch: {meta['gitBranch']}")
            else:
                latest = find_latest_claude_session(panel_cwd)
                if latest:
                    print(f"    session: {latest['sessionId']}")
                    print(f"    status: stopped (from index)")
                    if latest.get("summary"):
                        print(f"    summary: {latest['summary']}")
                else:
                    print(f"    status: stopped (no session found)")
        else:
            # Show running command for terminal panels
            cmds = terminal_cmds.get(panel_cwd, [])
            if cmds:
                cmd = cmds.pop(0)
                print(f"    command: {cmd}")
        print()


def _show_snapshot_workspace(ws, snap_path, timestamp, home):
    """Print detailed info for a snapshot workspace."""
    title = ws.get("title", "untitled")
    cwd = ws.get("cwd", "")

    print(f"Workspace: {title}")
    print(f"Directory: {cwd.replace(home, '~')}")
    print(f"Snapshot:  {os.path.basename(snap_path)} ({timestamp})")
    print(f"Panels:    {len(ws.get('panels', []))}")
    print()

    for i, panel in enumerate(ws.get("panels", [])):
        is_claude = panel.get("isClaude", False)
        panel_dir = panel.get("directory", "")
        short_dir = panel_dir.replace(home, "~")
        panel_title = panel.get("title", "")

        kind = "claude" if is_claude else "terminal"
        clean_title = panel_title.replace("\u2733 ", "").replace("\u2733", "").strip()
        if not clean_title:
            clean_title = "(untitled)"

        print(f"  Panel {i + 1}: [{kind}] {clean_title}")
        print(f"    cwd: {short_dir}")

        if is_claude:
            session = panel.get("claudeSession", {})
            if session.get("session_id"):
                print(f"    session: {session['session_id']}")
            if session.get("pid"):
                print(f"    pid: {session['pid']} (at snapshot time)")
            if session.get("summary"):
                print(f"    summary: {session['summary']}")
            if session.get("gitBranch"):
                print(f"    branch: {session['gitBranch']}")
            if session.get("note"):
                print(f"    note: {session['note']}")
        else:
            if panel.get("lastCommand"):
                print(f"    command: {panel['lastCommand']}")
        print()


def cmd_restore(args):
    """Restore cmux workspaces and Claude sessions from a snapshot."""
    if args.file:
        snap_path = args.file
    else:
        snap_path = os.path.join(SNAPSHOT_DIR, "latest.json")

    if not os.path.exists(snap_path):
        print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
        print("Run 'cmux-sessions snapshot' first to create one.", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snap = json.load(f)

    ws_filter = getattr(args, "workspace", None)
    run_commands = getattr(args, "run_commands", False)
    skip_active = getattr(args, "skip_active", False)
    home = os.path.expanduser("~")

    print(f"Restoring from: {snap_path}")
    print(f"Snapshot taken:  {snap.get('timestamp', 'unknown')}")
    if ws_filter:
        print(f"Workspace filter: {ws_filter}")
    print()

    # When --skip-active, determine which workspaces are already open
    active_titles = set()
    if skip_active:
        try:
            live_workspaces = _get_live_workspaces()
            active_titles = {ws["title"].lower() for ws in live_workspaces}
        except SystemExit:
            pass  # can't query cmux; proceed without filtering

    # Build ordered restore plan
    steps = []
    total_workspaces = 0
    total_panels = 0
    total_claude = 0
    skipped = []
    skipped_active = []
    matched_any = False

    for win in snap.get("windows", []):
        for ws_idx, ws in enumerate(win.get("workspaces", [])):
            title = ws.get("title", "untitled")

            # Apply workspace filter
            if ws_filter is not None:
                match = False
                if ws_filter.isdigit() and int(ws_filter) == ws.get("index", ws_idx):
                    match = True
                elif ws_filter.lower() in title.lower():
                    match = True
                if not match:
                    continue
            matched_any = True

            # Skip workspaces that are already open
            if skip_active and title.lower() in active_titles:
                skipped_active.append(title)
                continue

            cwd = ws.get("cwd", "")

            if not cwd or not os.path.isdir(cwd):
                skipped.append((title, cwd))
                continue

            total_workspaces += 1
            panels = ws.get("panels", [])
            layout = ws.get("layout", {})

            # Determine split order from layout
            split_ops = layout_to_splits(layout)

            # Step 1: Create workspace — use a shell var to capture its ref
            # so all subsequent commands target it, not the caller's workspace
            first_panel = panels[0] if panels else None
            first_cmd = _panel_command(first_panel, run_commands) if first_panel else None
            first_dir = first_panel.get("directory", cwd) if first_panel else cwd

            # Variable name unique per workspace to avoid collisions in multi-workspace restores
            ws_var = f"WS_{total_workspaces}"

            steps.append({
                "type": "workspace",
                "desc": f"Create workspace: {title}",
                "cmd": f"cmux new-workspace --cwd '{cwd}'",
                "title": title,
                "ws_var": ws_var,
            })

            # Capture the initial surface so we can target sends explicitly
            surf_counter = 0
            surf_var = f"S{total_workspaces}_{surf_counter}"
            steps.append({
                "type": "capture_surface",
                "desc": f"    Capture initial surface",
                "ws_var": ws_var,
                "surf_var": surf_var,
            })

            # Send command to the first pane — always cd to the panel's
            # directory first since the workspace shell may inherit the
            # caller's cwd rather than --cwd
            if first_cmd:
                if "&&" in first_cmd:
                    full_cmd = first_cmd
                elif first_cmd.startswith("cd "):
                    full_cmd = f"cd '{_sh_escape(first_dir)}'"
                else:
                    full_cmd = f"cd '{_sh_escape(first_dir)}' && {first_cmd}"
            else:
                full_cmd = f"cd '{_sh_escape(first_dir)}'"

            steps.append({
                "type": "send",
                "desc": f"    Launch: {full_cmd[:50]}",
                "cmd_tpl": f"cmux send --workspace \"${ws_var}\" --surface \"${surf_var}\" '{_sh_escape(full_cmd)}'",
                "enter": True,
                "ws_var": ws_var,
                "surf_var": surf_var,
            })

            if first_panel:
                total_panels += 1
                if first_panel.get("isClaude"):
                    total_claude += 1

            # Step 2: Rename workspace
            steps.append({
                "type": "rename",
                "desc": f"  Rename to: {title}",
                "cmd_tpl": f"cmux rename-workspace --workspace \"${ws_var}\" '{_sh_escape(title)}'",
                "ws_var": ws_var,
            })

            # Step 3: Create splits for remaining panes
            # Map panel IDs to panel data
            panels_by_id = {p.get("id"): p for p in panels}

            # Use split_ops to determine split directions; skip first (it's the initial pane)
            for i, op in enumerate(split_ops):
                if i == 0:
                    continue  # first pane is the workspace default

                direction = op.get("direction", "right")
                if not direction:
                    direction = "right"

                # Find the panel data for this split
                panel = None
                for pid in op.get("panelIds", []):
                    if pid in panels_by_id:
                        panel = panels_by_id[pid]
                        break

                if not panel:
                    # Fallback: use panels in order
                    if i < len(panels):
                        panel = panels[i]

                if not panel:
                    continue

                panel_dir = panel.get("directory", cwd)
                panel_cmd = _panel_command(panel, run_commands)

                # Create the split, then capture the new surface ref
                surf_counter += 1
                surf_var = f"S{total_workspaces}_{surf_counter}"

                steps.append({
                    "type": "split",
                    "desc": f"  Split {direction}: {panel.get('title', 'panel')[:40]}",
                    "cmd_tpl": f"cmux new-split {direction} --workspace \"${ws_var}\"",
                    "ws_var": ws_var,
                })

                steps.append({
                    "type": "capture_surface",
                    "desc": f"    Capture new surface",
                    "ws_var": ws_var,
                    "surf_var": surf_var,
                })

                # Send command to the new surface — always cd to panel dir
                if panel_cmd and "&&" in panel_cmd:
                    # Compound command (e.g. cd ... && npm run dev) — use as-is
                    full_cmd = panel_cmd
                elif panel_cmd and not panel_cmd.startswith("cd "):
                    full_cmd = f"cd '{_sh_escape(panel_dir)}' && {panel_cmd}"
                else:
                    full_cmd = f"cd '{_sh_escape(panel_dir)}'"

                steps.append({
                    "type": "send",
                    "desc": f"    Launch: {full_cmd[:50]}",
                    "cmd_tpl": f"cmux send --workspace \"${ws_var}\" --surface \"${surf_var}\" '{_sh_escape(full_cmd)}'",
                    "enter": True,
                    "ws_var": ws_var,
                    "surf_var": surf_var,
                })

                total_panels += 1
                if panel.get("isClaude"):
                    total_claude += 1

    if ws_filter and not matched_any:
        print(f"Error: No workspace matching '{ws_filter}' found in snapshot.", file=sys.stderr)
        # List available workspaces to help the user
        print("Available workspaces:", file=sys.stderr)
        for w in snap.get("windows", []):
            for ws in w.get("workspaces", []):
                print(f"  [{ws.get('index', '?')}] {ws.get('title', 'untitled')}", file=sys.stderr)
        sys.exit(1)

    # Print plan
    if skipped_active:
        print(f"Skipped (already open): {len(skipped_active)} workspace(s)")
        for t in skipped_active:
            print(f"  - {t}")
        print()

    if skipped:
        print("Skipped (directory not found):")
        for title, cwd in skipped:
            print(f"  {title}: {cwd}")
        print()

    non_claude = total_panels - total_claude

    # Collect workspace titles for display (exclude skipped-active)
    skipped_active_lower = {t.lower() for t in skipped_active}
    ws_titles = [
        ws.get("title", "untitled")
        for win in snap.get("windows", [])
        for ws in win.get("workspaces", [])
        if _snap_ws_matches(ws, ws_filter)
        and ws.get("title", "untitled").lower() not in skipped_active_lower
    ]

    if total_workspaces == 0:
        if skipped_active:
            print("All matching workspaces are already open. Nothing to restore.")
        else:
            print("No workspaces to restore.")
        return

    print(f"Plan: {total_workspaces} workspaces, {total_panels} panels ({total_claude} Claude, {non_claude} terminal)")
    for t in ws_titles:
        print(f"  - {t}")
    print()

    # Show saved commands as hints
    saved_cmds = []
    for win in snap.get("windows", []):
        for ws in win.get("workspaces", []):
            if not _snap_ws_matches(ws, ws_filter):
                continue
            for p in ws.get("panels", []):
                if p.get("lastCommand") and not p.get("isClaude"):
                    short_dir = p.get("directory", "").replace(home, "~")
                    saved_cmds.append((ws.get("title", ""), short_dir, p["lastCommand"]))

    if saved_cmds and not run_commands:
        print("Saved terminal commands (not restored by default):")
        for ws_title, pdir, cmd in saved_cmds:
            print(f"  {pdir}: {cmd}")
        print(f"  Use --run-commands to auto-run these on restore.")
        print()
    elif saved_cmds and run_commands:
        print("Terminal commands will be re-run:")
        for ws_title, pdir, cmd in saved_cmds:
            print(f"  {pdir}: {cmd}")
        print()

    if args.dry_run:
        _print_dry_run(steps)
        return

    if _inside_cmux():
        # Check for already-open workspaces that would conflict
        already_open = []
        try:
            live_workspaces = _get_live_workspaces()
            live_titles = {ws["title"].lower() for ws in live_workspaces}
            already_open = [t for t in ws_titles if t.lower() in live_titles]
        except SystemExit:
            pass  # _get_live_workspaces calls sys.exit on failure; ignore here

        if already_open:
            print(f"WARNING: {len(already_open)} workspace(s) already open:")
            for t in already_open:
                print(f"  - {t}")
            print()
            print("Restoring would create duplicates and conflict with active Claude sessions.")
            print("Kill them first with: make kill W=<name>")
            print("Or use respawn:       make respawn W=<name>")
            print()
            try:
                answer = input("Restore anyway? Type 'force' to continue: ")
                if answer.strip().lower() != "force":
                    print("Aborted.")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
        elif ws_filter is None and total_workspaces > 1:
            # Stronger warning when restoring all workspaces
            print("WARNING: No workspace filter specified — this will restore ALL workspaces.")
            try:
                answer = input(f"Restore all {total_workspaces} workspaces? Type 'yes' to confirm: ")
                if answer.strip().lower() != "yes":
                    print("Aborted.")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return
        else:
            try:
                answer = input("Restore now? [y/N] ")
                if answer.strip().lower() not in ("y", "yes"):
                    print("Aborted.")
                    return
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return

        _execute_restore(steps, total_workspaces, total_panels, total_claude, non_claude)
    else:
        _generate_restore_script(
            steps, snap_path, total_workspaces, total_panels, total_claude, non_claude
        )


def _print_dry_run(steps):
    """Print a dry-run preview of restore steps."""
    print("Steps:")
    print()
    for s in steps:
        step_type = s["type"]
        if step_type == "capture_surface":
            ws_var = s.get("ws_var", "")
            surf_var = s.get("surf_var", "")
            print(f"  [   capture] {surf_var}=$(cmux list-panels --workspace \"${ws_var}\" | grep -o 'surface:[0-9]*' | tail -1)")
            print()
            continue
        cmd = s.get("cmd_tpl", s.get("cmd", ""))
        print(f"  [{step_type:>10}] {s['desc']}")
        if step_type == "workspace":
            ws_var = s.get("ws_var", "WS")
            print(f"             $ {s['cmd']}")
            print(f"             $ {ws_var}=$(cmux list-workspaces | tail -1 | awk '{{print $1}}')")
        else:
            print(f"             $ {cmd}")
        if s.get("enter"):
            ws_var = s.get("ws_var", "")
            surf_var = s.get("surf_var", "")
            surf_part = f" --surface \"${surf_var}\"" if surf_var else ""
            print(f"             $ cmux send-key --workspace \"${ws_var}\"{surf_part} Enter")
        print()
    print("(dry run — no changes made)")


def _run_cmux(args, timeout=10):
    """Run a cmux command and return (success, stdout, stderr)."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return False, "", str(e)


def _get_surface_refs(ws_ref):
    """Get all surface refs for a workspace."""
    ok, out, err = _run_cmux(["cmux", "list-panels", "--workspace", ws_ref])
    if not ok or not out:
        return []
    refs = []
    for line in out.strip().splitlines():
        match = re.search(r'(surface:\d+)', line)
        if match:
            refs.append(match.group(1))
    return refs


def _execute_restore(steps, total_workspaces, total_panels, total_claude, non_claude):
    """Execute restore steps directly via cmux CLI."""
    import time

    ws_refs = {}   # ws_var -> resolved workspace ref
    surf_refs = {} # surf_var -> resolved surface ref
    known_surfaces = set()  # track surfaces we've already seen

    for s in steps:
        step_type = s["type"]
        ws_var = s.get("ws_var", "")
        surf_var = s.get("surf_var", "")

        if step_type == "capture_surface":
            # Don't print a line for this internal step
            ws_ref = ws_refs.get(ws_var, "")
            if ws_ref:
                current = _get_surface_refs(ws_ref)
                # Find new surfaces we haven't seen before
                new_refs = [r for r in current if r not in known_surfaces]
                if new_refs:
                    ref = new_refs[-1]  # newest
                else:
                    ref = current[-1] if current else ""  # fallback to last
                if ref:
                    surf_refs[surf_var] = ref
                    known_surfaces.add(ref)
                    print(f"             surface: {ref}")
                else:
                    print(f"    WARNING: No surface refs found")
            continue

        print(f"  [{step_type:>10}] {s['desc']}")

        if step_type == "workspace":
            ok, out, err = _run_cmux(s["cmd"].split())
            if not ok:
                print(f"    ERROR: {err}")
                sys.exit(1)
            time.sleep(1)

            # Capture the new workspace ref
            ok, out, err = _run_cmux(["cmux", "list-workspaces"])
            if ok and out:
                last_line = out.strip().splitlines()[-1]
                ref = last_line.split()[0] if last_line else ""
                ws_refs[ws_var] = ref
                print(f"             ref: {ref}")
            else:
                print(f"    WARNING: Could not capture workspace ref: {err}")

        else:
            # Resolve variable references in the command
            cmd_str = s.get("cmd_tpl", s.get("cmd", ""))
            if ws_var and ws_var in ws_refs:
                cmd_str = cmd_str.replace(f'"${ws_var}"', ws_refs[ws_var])
            if surf_var and surf_var in surf_refs:
                cmd_str = cmd_str.replace(f'"${surf_var}"', surf_refs[surf_var])

            ok, out, err = _run_cmux(["bash", "-c", cmd_str])
            if not ok:
                print(f"    WARNING: {err}")

            if s.get("enter"):
                ws_ref = ws_refs.get(ws_var, "")
                surf_ref = surf_refs.get(surf_var, "")
                send_key_args = ["cmux", "send-key"]
                if ws_ref:
                    send_key_args += ["--workspace", ws_ref]
                if surf_ref:
                    send_key_args += ["--surface", surf_ref]
                send_key_args.append("Enter")
                _run_cmux(send_key_args)

            # Pacing
            if step_type == "split":
                time.sleep(0.3)
            elif step_type == "send":
                time.sleep(0.5)

    print()
    print(f"Restored {total_workspaces} workspaces, {total_panels} panels ({total_claude} Claude, {non_claude} terminal).")


def _generate_restore_script(steps, snap_path, total_workspaces, total_panels, total_claude, non_claude):
    """Generate a restore shell script for running outside cmux."""
    script_path = os.path.join(SNAPSHOT_DIR, "restore.sh")
    with open(script_path, "w") as f:
        f.write("#!/bin/bash\n")
        f.write(f"# cmux workspace restore script\n")
        f.write(f"# Generated: {datetime.now().isoformat()}\n")
        f.write(f"# Source: {snap_path}\n")
        f.write(f"# Workspaces: {total_workspaces}, Panels: {total_panels} ({total_claude} Claude, {non_claude} terminal)\n")
        f.write("#\n")
        f.write("# Run this from inside a cmux terminal.\n")
        f.write("# Review with --dry-run first: cmux-sessions restore --dry-run\n\n")
        f.write("set -e\n\n")

        for s in steps:
            step_type = s["type"]

            if step_type == "capture_surface":
                ws_var = s.get("ws_var", "")
                surf_var = s.get("surf_var", "")
                f.write(f"# Capture surface ref\n")
                f.write(f"{surf_var}=$(cmux list-panels --workspace \"${ws_var}\" | grep -o 'surface:[0-9]*' | tail -1)\n")
                f.write("\n")
                continue

            cmd = s.get("cmd_tpl", s.get("cmd", ""))
            f.write(f"# {s['desc']}\n")

            if step_type == "workspace":
                ws_var = s.get("ws_var", "WS")
                f.write(f"{s['cmd']}\n")
                f.write("sleep 1\n")
                f.write(f"{ws_var}=$(cmux list-workspaces | tail -1 | awk '{{print $1}}')\n")
                f.write(f'echo "  Workspace ref: ${ws_var}"\n')
            else:
                f.write(f"{cmd}\n")

            if s.get("enter"):
                ws_var = s.get("ws_var", "")
                surf_var = s.get("surf_var", "")
                surf_part = f" --surface \"${surf_var}\"" if surf_var else ""
                f.write(f"cmux send-key --workspace \"${ws_var}\"{surf_part} Enter\n")

            if step_type == "split":
                f.write("sleep 0.3\n")
            elif step_type == "send":
                f.write("sleep 0.5\n")

            f.write("\n")

        f.write(f'echo "Restored {total_workspaces} workspaces, {total_panels} panels ({total_claude} Claude, {non_claude} terminal)."\n')

    os.chmod(script_path, 0o755)

    print(f"Not running inside cmux — generated restore script instead.")
    print()
    print(f"  {script_path}")
    print()
    print("Run it from inside a cmux terminal, or review first with:")
    print(f"  cmux-sessions restore --dry-run")


def _panel_command(panel, run_commands=False):
    """Determine the shell command to launch in a panel."""
    if not panel:
        return None

    if panel.get("isClaude"):
        session = panel.get("claudeSession", {})
        session_id = session.get("session_id")
        if session_id:
            return f"claude --resume {session_id}"
        else:
            return "claude -c"

    # Non-Claude panels: cd to their working directory
    panel_dir = panel.get("directory", "")
    last_cmd = panel.get("lastCommand", "")

    if run_commands and last_cmd and panel_dir:
        return f"cd '{_sh_escape(panel_dir)}' && {last_cmd}"
    elif panel_dir:
        return f"cd '{_sh_escape(panel_dir)}'"

    return None


def _sh_escape(s):
    """Escape single quotes for shell embedding."""
    return s.replace("'", "'\\''")


def cmd_snapshots(args):
    """List available snapshots."""
    if not os.path.isdir(SNAPSHOT_DIR):
        print("No snapshots found.")
        return

    files = sorted(Path(SNAPSHOT_DIR).glob("cmux-*.json"), reverse=True)
    if not files:
        print("No snapshots found.")
        return

    print(f"{'SNAPSHOT':<35} {'WORKSPACES':<35} {'CLAUDE':>6} {'PANELS':>6}")
    print(f"{'-'*35} {'-'*35} {'-'*6} {'-'*6}")

    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
            ws_names = [
                ws.get("title", "untitled")
                for w in data.get("windows", [])
                for ws in w["workspaces"]
            ]
            names_str = ", ".join(ws_names) if ws_names else "(none)"
            if len(names_str) > 33:
                names_str = names_str[:30] + "..."
            n_panels = sum(
                len(ws["panels"])
                for w in data.get("windows", [])
                for ws in w["workspaces"]
            )
            n_claude = sum(
                1 for w in data.get("windows", [])
                for ws in w["workspaces"]
                for p in ws["panels"] if p.get("isClaude")
            )
            print(f"{fp.name:<35} {names_str:<35} {n_claude:>6} {n_panels:>6}")
        except Exception:
            print(f"{fp.name:<35} {'(corrupt)':<35}")


def cmd_validate(args):
    """Check if a snapshot is still valid for restoring."""
    home = os.path.expanduser("~")

    if args.file:
        snap_path = args.file
    else:
        snap_path = os.path.join(SNAPSHOT_DIR, "latest.json")

    if not os.path.exists(snap_path):
        print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snap = json.load(f)

    ws_filter = getattr(args, "workspace", None)

    print(f"Validating: {snap_path}")
    print(f"Snapshot:   {snap.get('timestamp', 'unknown')}")
    if ws_filter:
        print(f"Filter:     {ws_filter}")
    print()

    all_pass = True
    rows = []

    for win in snap.get("windows", []):
        for ws in win.get("workspaces", []):
            if not _snap_ws_matches(ws, ws_filter):
                continue

            ws_title = ws.get("title", "untitled")
            ws_cwd = ws.get("cwd", "")

            # Check workspace directory
            ws_dir_ok = os.path.isdir(ws_cwd) if ws_cwd else False
            if not ws_dir_ok:
                all_pass = False

            for i, panel in enumerate(ws.get("panels", [])):
                panel_dir = panel.get("directory", "")
                short_dir = panel_dir.replace(home, "~")
                is_claude = panel.get("isClaude", False)

                # Directory check
                dir_ok = os.path.isdir(panel_dir) if panel_dir else False
                if not dir_ok:
                    all_pass = False

                # Session check (Claude only)
                session_ok = None
                if is_claude:
                    session = panel.get("claudeSession", {})
                    sid = session.get("session_id", "")
                    if sid and panel_dir:
                        encoded = panel_dir.replace("/", "-")
                        jsonl = os.path.join(CLAUDE_PROJECTS_DIR, encoded, f"{sid}.jsonl")
                        session_ok = os.path.exists(jsonl)
                        if not session_ok:
                            all_pass = False
                    else:
                        session_ok = False
                        all_pass = False

                kind = "claude" if is_claude else "terminal"
                dir_str = "\033[32mPASS\033[0m" if dir_ok else "\033[31mFAIL\033[0m"
                if session_ok is None:
                    sess_str = "-"
                elif session_ok:
                    sess_str = "\033[32mPASS\033[0m"
                else:
                    sess_str = "\033[31mFAIL\033[0m"

                rows.append((ws_title, f"[{kind}]", short_dir, dir_str, sess_str))

    if not rows:
        if ws_filter:
            print(f"No workspace matching '{ws_filter}' in snapshot.")
        else:
            print("No panels found in snapshot.")
        sys.exit(1)

    headers = ("WORKSPACE", "TYPE", "DIRECTORY", "DIR", "SESSION")
    # Calculate widths without ANSI codes
    def strip_ansi(s):
        return re.sub(r'\033\[[0-9;]*m', '', s)

    widths = [max(len(h), max(len(strip_ansi(r[i])) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)

    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        # Pad with ANSI-aware widths
        parts = []
        for i, val in enumerate(row):
            visible_len = len(strip_ansi(val))
            padding = widths[i] - visible_len
            parts.append(val + " " * padding)
        print("  ".join(parts))

    print()
    if all_pass:
        print("\033[32mAll checks passed.\033[0m")
    else:
        print("\033[31mSome checks failed.\033[0m Review before restoring.")
        sys.exit(1)


def cmd_diff(args):
    """Compare a snapshot against live cmux workspaces."""
    if args.file:
        snap_path = args.file
    else:
        snap_path = os.path.join(SNAPSHOT_DIR, "latest.json")

    if not os.path.exists(snap_path):
        print(f"Error: Snapshot not found at {snap_path}", file=sys.stderr)
        sys.exit(1)

    with open(snap_path) as f:
        snap = json.load(f)

    # Get snapshot workspace titles
    snap_workspaces = {}
    for win in snap.get("windows", []):
        for ws in win.get("workspaces", []):
            title = ws.get("title", "untitled")
            panels = ws.get("panels", [])
            claude_count = sum(1 for p in panels if p.get("isClaude"))
            snap_workspaces[title] = {
                "panels": len(panels),
                "claude": claude_count,
                "terminal": len(panels) - claude_count,
            }

    # Get live workspace titles
    try:
        live_workspaces = _get_live_workspaces()
        live_titles = {ws["title"] for ws in live_workspaces}
    except SystemExit:
        print("Error: Cannot query live workspaces. Are you inside cmux?", file=sys.stderr)
        sys.exit(1)

    # Categorize
    snap_titles = set(snap_workspaces.keys())
    active = snap_titles & live_titles
    closed = snap_titles - live_titles
    extra = live_titles - snap_titles

    print(f"Snapshot:  {os.path.basename(snap_path)} ({snap.get('timestamp', 'unknown')})")
    print(f"Total:     {len(snap_titles)} in snapshot, {len(live_titles)} live")
    print()

    if closed:
        print(f"Closed ({len(closed)}) — in snapshot but not running:")
        for t in sorted(closed):
            info = snap_workspaces[t]
            print(f"  - {t}  ({info['panels']} panels: {info['claude']}C/{info['terminal']}T)")
        print()

    if active:
        print(f"Active ({len(active)}) — in snapshot and currently running:")
        for t in sorted(active):
            info = snap_workspaces[t]
            print(f"  - {t}  ({info['panels']} panels: {info['claude']}C/{info['terminal']}T)")
        print()

    if extra:
        print(f"New ({len(extra)}) — running but not in snapshot:")
        for t in sorted(extra):
            print(f"  - {t}")
        print()

    if not closed:
        print("All snapshot workspaces are currently active.")
    else:
        print(f"To restore closed workspaces: make restore SA=1" + (f" F={os.path.basename(snap_path).replace('.json', '')}" if args.file else ""))


def cmd_prune(args):
    """Delete old snapshots, keeping the most recent N."""
    if not os.path.isdir(SNAPSHOT_DIR):
        print("No snapshots found.")
        return

    files = sorted(Path(SNAPSHOT_DIR).glob("cmux-*.json"), reverse=True)
    if not files:
        print("No snapshots found.")
        return

    keep = args.keep
    to_delete = files[keep:]

    if not to_delete:
        print(f"Nothing to prune ({len(files)} snapshots, keeping {keep}).")
        return

    print(f"Snapshots: {len(files)} total, keeping {keep}, deleting {len(to_delete)}")
    for fp in to_delete:
        print(f"  delete: {fp.name}")
        os.remove(fp)

    print(f"\nPruned {len(to_delete)} snapshots.")


def _snap_ws_matches(ws, ws_filter):
    """Check if a snapshot workspace entry matches the filter."""
    if ws_filter is None:
        return True
    title = ws.get("title", "untitled")
    idx = ws.get("index", -1)
    if ws_filter.isdigit() and int(ws_filter) == idx:
        return True
    return ws_filter.lower() in title.lower()


def _inside_cmux():
    """Check if we're running inside a cmux terminal."""
    return bool(os.environ.get("CMUX_WORKSPACE_ID"))


def _get_live_workspaces():
    """Query cmux for currently open workspaces. Returns list of dicts with id, title, index."""
    try:
        result = subprocess.run(
            ["cmux", "list-workspaces"],
            capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        print(f"Error: Cannot talk to cmux ({e}). Are you inside a cmux terminal?", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"Error: cmux list-workspaces failed: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    workspaces = []
    for line in result.stdout.strip().splitlines():
        # cmux list-workspaces outputs lines like: workspace:0  Title Here
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        ref = parts[0]
        title = parts[1]
        workspaces.append({"ref": ref, "title": title})
    return workspaces


def _find_workspace_ref(ws_filter):
    """Find a live workspace ref matching the filter. Returns (ref, title) or exits with error."""
    workspaces = _get_live_workspaces()
    matches = []
    for ws in workspaces:
        if ws_filter.lower() in ws["title"].lower():
            matches.append(ws)
        elif ws["ref"] == ws_filter:
            matches.append(ws)

    if not matches:
        print(f"Error: No open workspace matching '{ws_filter}'.", file=sys.stderr)
        print("Open workspaces:", file=sys.stderr)
        for ws in workspaces:
            print(f"  {ws['ref']}  {ws['title']}", file=sys.stderr)
        sys.exit(1)

    if len(matches) > 1:
        print(f"Error: Multiple workspaces match '{ws_filter}':", file=sys.stderr)
        for ws in matches:
            print(f"  {ws['ref']}  {ws['title']}", file=sys.stderr)
        print("Be more specific.", file=sys.stderr)
        sys.exit(1)

    return matches[0]["ref"], matches[0]["title"]


def cmd_kill(args):
    """Close a cmux workspace after confirmation."""
    if not args.workspace:
        print("Error: -w/--workspace is required for kill.", file=sys.stderr)
        sys.exit(1)

    ref, title = _find_workspace_ref(args.workspace)

    # Show what will be killed
    print(f"Workspace:  {title}")
    print(f"Ref:        {ref}")

    # Count panels from cmux session data
    cmux_data = load_cmux_session()
    panel_count = 0
    claude_count = 0
    for win in cmux_data.get("windows", []):
        for ws in win.get("tabManager", {}).get("workspaces", []):
            ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
            if args.workspace.lower() in ws_title.lower():
                panels = ws.get("panels", [])
                panel_count = len(panels)
                claude_count = sum(
                    1 for p in panels
                    if "Claude" in p.get("title", "") or p.get("title", "").startswith("\u2733")
                )
                break

    print(f"Panels:     {panel_count} ({claude_count} Claude, {panel_count - claude_count} terminal)")
    print()

    if args.yes:
        confirmed = True
    else:
        try:
            answer = input(f"Kill workspace '{title}'? This will close all panels. [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

    if not confirmed:
        print("Aborted.")
        return

    try:
        result = subprocess.run(
            ["cmux", "close-workspace", "--workspace", ref],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            print(f"Error: cmux close-workspace failed: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error closing workspace: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Killed workspace '{title}'.")


def cmd_respawn(args):
    """Snapshot, kill, and restore a workspace in one step."""
    if not args.workspace:
        print("Error: -w/--workspace is required for respawn.", file=sys.stderr)
        sys.exit(1)

    ws_filter = args.workspace

    # Step 1: Verify workspace exists before we start
    ref, title = _find_workspace_ref(ws_filter)

    # Show what will happen
    cmux_data = load_cmux_session()
    panel_count = 0
    claude_count = 0
    for win in cmux_data.get("windows", []):
        for ws in win.get("tabManager", {}).get("workspaces", []):
            ws_title = ws.get("customTitle") or ws.get("title") or os.path.basename(ws.get("currentDirectory", ""))
            if ws_filter.lower() in ws_title.lower():
                panels = ws.get("panels", [])
                panel_count = len(panels)
                claude_count = sum(
                    1 for p in panels
                    if "Claude" in p.get("title", "") or p.get("title", "").startswith("\u2733")
                )
                break

    print(f"Respawn workspace: {title}")
    print(f"  Panels: {panel_count} ({claude_count} Claude, {panel_count - claude_count} terminal)")
    print(f"  This will: snapshot → kill → restore")
    print()

    if args.yes:
        confirmed = True
    else:
        try:
            answer = input(f"Respawn '{title}'? [y/N] ")
            confirmed = answer.strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)

    if not confirmed:
        print("Aborted.")
        return

    # Step 1: Snapshot
    print(f"\n[1/3] Snapshotting '{title}'...")
    snap_args = argparse.Namespace(workspace=ws_filter, output=None)
    cmd_snapshot(snap_args)

    # Step 2: Kill
    print(f"\n[2/3] Killing '{title}'...")
    kill_args = argparse.Namespace(workspace=ws_filter, yes=True)
    cmd_kill(kill_args)

    # Step 3: Restore
    print(f"\n[3/3] Restoring '{title}'...")
    restore_args = argparse.Namespace(workspace=ws_filter, file=None, dry_run=False)
    cmd_restore(restore_args)


# ── Main ─────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Snapshot and restore cmux workspaces with Claude Code sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s list                            Show Claude sessions across workspaces
  %(prog)s show -w myproject               Show detailed workspace info (live)
  %(prog)s show -w myproject -f snap.json  Show detailed workspace info (snapshot)
  %(prog)s snapshot                        Save current state (all workspaces)
  %(prog)s snapshot -w myproject           Snapshot only matching workspace
  %(prog)s snapshot -n before-refactor     Save with a name instead of timestamp
  %(prog)s snapshots                       List all saved snapshots
  %(prog)s diff                            Compare snapshot vs live workspaces
  %(prog)s validate                        Check snapshot health before restoring
  %(prog)s validate -f snap.json -w proj   Validate specific snapshot/workspace
  %(prog)s prune                           Delete old snapshots (keep last 10)
  %(prog)s prune --keep 5                  Keep last 5 snapshots
  %(prog)s restore --dry-run               Preview what would be restored
  %(prog)s restore -w myproject            Restore only matching workspace
  %(prog)s restore --skip-active           Restore only closed workspaces
  %(prog)s restore --run-commands          Re-run captured terminal commands
  %(prog)s kill -w myproject               Close a workspace (with confirmation)
  %(prog)s respawn -w myproject            Snapshot, kill, and restore a workspace
""",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="List active Claude sessions in cmux")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Capture current cmux + Claude state")
    p_snap.add_argument("-o", "--output", help="Output file (default: ~/.cmux-snapshots/cmux-<timestamp>.json)")
    p_snap.add_argument("-w", "--workspace", help="Snapshot only this workspace (name substring or index)")
    p_snap.add_argument("-n", "--name", help="Give the snapshot a name (e.g. before-refactor)")

    # show
    p_show = sub.add_parser("show", help="Show detailed workspace info")
    p_show.add_argument("-w", "--workspace", help="Workspace name (substring match)")
    p_show.add_argument("-f", "--file", help="Show from snapshot file instead of live state")

    # snapshots
    sub.add_parser("snapshots", help="List available snapshots")

    # restore
    p_restore = sub.add_parser("restore", help="Restore workspaces from a snapshot")
    p_restore.add_argument("-f", "--file", help="Snapshot file (default: latest)")
    p_restore.add_argument("-w", "--workspace", help="Restore only this workspace (name substring or index)")
    p_restore.add_argument("--dry-run", action="store_true", help="Preview without executing")
    p_restore.add_argument("--run-commands", action="store_true", help="Re-run captured terminal commands (use with caution)")
    p_restore.add_argument("--skip-active", action="store_true", help="Skip workspaces that are already open (restore only closed ones)")

    # diff
    p_diff = sub.add_parser("diff", help="Compare snapshot against live workspaces")
    p_diff.add_argument("-f", "--file", help="Snapshot file (default: latest)")

    # validate
    p_val = sub.add_parser("validate", help="Check snapshot health before restoring")
    p_val.add_argument("-f", "--file", help="Snapshot file (default: latest)")
    p_val.add_argument("-w", "--workspace", help="Validate only this workspace")

    # prune
    p_prune = sub.add_parser("prune", help="Delete old snapshots, keep last N")
    p_prune.add_argument("--keep", type=int, default=10, help="Number of snapshots to keep (default: 10)")

    # kill
    p_kill = sub.add_parser("kill", help="Close a workspace (with confirmation)")
    p_kill.add_argument("-w", "--workspace", required=True, help="Workspace to close (name substring or ref)")
    p_kill.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    # respawn
    p_respawn = sub.add_parser("respawn", help="Snapshot, kill, and restore a workspace")
    p_respawn.add_argument("-w", "--workspace", required=True, help="Workspace to respawn (name substring)")
    p_respawn.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()
    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "snapshot": cmd_snapshot,
        "snapshots": cmd_snapshots,
        "diff": cmd_diff,
        "validate": cmd_validate,
        "prune": cmd_prune,
        "restore": cmd_restore,
        "kill": cmd_kill,
        "respawn": cmd_respawn,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
