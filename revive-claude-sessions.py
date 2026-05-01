#!/usr/bin/env python3
"""
Revive Claude Code sessions captured in a cmux-sessions snapshot but lost on restore.

For each Claude session in the snapshot:
  * Locate its JSONL on disk (~/.claude/projects/*/<session_id>.jsonl)
  * Extract title, last activity, size
  * Filter by --since (hours) and --min-size (KB)
  * Find or create the cmux workspace by original title
  * Open a new pane and run `claude --resume <session_id>`

Usage:
  revive-claude-sessions.py --dry-run                 # default cutoff: 24h, min 50KB
  revive-claude-sessions.py --since 12 --min-size 100 --dry-run
  revive-claude-sessions.py --since 24 --apply        # actually do it
  revive-claude-sessions.py -f /path/to/snap.json --dry-run
"""
import argparse, glob, json, os, re, shlex, subprocess, sys, time

SNAP_DIR = os.path.expanduser("~/.cmux-snapshots")


def load_snapshot(path):
    if not path:
        path = os.path.join(SNAP_DIR, "latest.json")
    with open(path) as f:
        return path, json.load(f)


def find_jsonl(cwd, sid):
    """Locate <session_id>.jsonl. Try cwd-encoded dir first, then any project dir."""
    if cwd:
        encoded = cwd.replace("/", "-")
        p = os.path.expanduser(f"~/.claude/projects/{encoded}/{sid}.jsonl")
        if os.path.exists(p):
            return p
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl"))
    return hits[0] if hits else None


def jsonl_meta(path):
    """Pull custom-title + first user message + git branch from a Claude JSONL."""
    title = ""
    first_user = ""
    git_branch = ""
    try:
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t == "custom-title" and not title:
                    title = rec.get("customTitle", "")
                elif t == "user" and not first_user:
                    msg = rec.get("message", {})
                    if msg.get("role") == "user":
                        c = msg.get("content", "")
                        if isinstance(c, str):
                            first_user = c[:120]
                        elif isinstance(c, list) and c and isinstance(c[0], dict):
                            first_user = str(c[0].get("text", ""))[:120]
                if not git_branch and rec.get("gitBranch"):
                    git_branch = rec["gitBranch"]
                if title and first_user and git_branch:
                    break
    except OSError:
        pass
    return title, first_user, git_branch


def extract_sessions(snap):
    out = []
    for w in snap.get("windows", []):
        for ws in w.get("workspaces", []):
            for p in ws.get("panels", []):
                cs = p.get("claudeSession")
                if not cs:
                    continue
                sid = cs.get("session_id")
                if not sid:
                    continue
                cwd = p.get("directory") or ws.get("cwd") or ""
                out.append({
                    "workspace": ws.get("title", "untitled"),
                    "cwd": cwd,
                    "sid": sid,
                })
    return out


def enrich(sessions):
    enriched = []
    for s in sessions:
        jsonl = find_jsonl(s["cwd"], s["sid"])
        if not jsonl:
            continue
        title, first_user, branch = jsonl_meta(jsonl)
        enriched.append({
            **s,
            "jsonl": jsonl,
            "title": title,
            "first_user": first_user,
            "branch": branch,
            "size": os.path.getsize(jsonl),
            "mtime": os.path.getmtime(jsonl),
        })
    return enriched


def live_workspaces():
    """List workspaces across all cmux windows (workspace.list defaults to current window)."""
    win_out = subprocess.check_output(["cmux", "list-windows"]).decode()
    win_ids = re.findall(r"\b([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})\b", win_out)
    seen = set()
    workspaces = []
    for wid in win_ids:
        try:
            out = subprocess.check_output(
                ["cmux", "rpc", "workspace.list", json.dumps({"window_id": wid})],
                stderr=subprocess.DEVNULL,
            ).decode()
            for w in json.loads(out).get("workspaces", []):
                if w.get("id") and w["id"] not in seen:
                    seen.add(w["id"])
                    w["window_id"] = wid
                    workspaces.append(w)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
    return workspaces


def find_live_ws(title, ws_list):
    """Return workspace dict for an exact-title match, else None."""
    for w in ws_list:
        if w.get("title", "") == title:
            return w
    return None


def label(e):
    age_h = (time.time() - e["mtime"]) / 3600
    size_kb = e["size"] // 1024
    desc = e["title"] or e["first_user"] or "(no preview)"
    return f"[{e['workspace'][:18]:<18}] {e['sid'][:8]} {age_h:5.1f}h {size_kb:5}KB  {desc[:70]}"


def build_plan(candidates, ws_list):
    """For each candidate session, decide: existing-ws split, or create-ws + send."""
    plan = []
    # Pre-group by workspace title so we don't keep re-querying
    by_ws = {}
    for c in candidates:
        by_ws.setdefault(c["workspace"], []).append(c)

    for ws_title, items in by_ws.items():
        live = find_live_ws(ws_title, ws_list)
        for i, c in enumerate(items):
            cmd = f"cd {shlex.quote(c['cwd'])} && claude --resume {c['sid']}"
            if live:
                plan.append({
                    "kind": "tab-into",
                    "ws_ref": live["ref"],
                    "ws_title": ws_title,
                    "cmd": cmd,
                    "session": c,
                })
            else:
                plan.append({
                    "kind": "create-and-send" if i == 0 else "tab-into-pending",
                    "ws_title": ws_title,
                    "cwd": c["cwd"],
                    "cmd": cmd,
                    "session": c,
                })
    return plan


def render_dry_run(plan):
    print()
    print(f"{'#':>3}  {'action':<22}  {'workspace':<22}  session")
    print("-" * 110)
    for i, step in enumerate(plan, 1):
        ws = step.get("ws_ref") or step["ws_title"]
        sid = step["session"]["sid"][:8]
        desc = step["session"]["title"] or step["session"]["first_user"] or ""
        print(f"{i:>3}  {step['kind']:<22}  {ws[:22]:<22}  {sid}  {desc[:50]}")
    print()
    print(f"Total: {len(plan)} pane(s) across {len({s['ws_title'] for s in plan})} workspace(s)")


def execute_plan(plan, sleep_send=0.6, sleep_split=0.4):
    """Apply the plan via cmux CLI. Mirrors cmux-sessions restore semantics."""
    created = {}  # ws_title -> ws_ref
    for step in plan:
        kind = step["kind"]
        cmd = step["cmd"]

        if kind == "create-and-send":
            title = step["ws_title"]
            cwd = step["cwd"]
            r = subprocess.run(
                ["cmux", "new-workspace", "--cwd", cwd],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                print(f"  ERROR creating workspace {title!r}: {r.stderr.strip()}")
                continue
            time.sleep(0.8)
            # Capture ref via list-workspaces — pick the workspace:N token, not the leading marker
            lw = subprocess.check_output(["cmux", "list-workspaces"]).decode()
            m = re.search(r"workspace:\d+", lw.splitlines()[-1])
            if not m:
                print(f"  WARNING: no ref captured for {title!r}")
                continue
            ws_ref = m.group(0)
            created[title] = ws_ref
            subprocess.run(["cmux", "rename-workspace", "--workspace", ws_ref, title])
            _send(ws_ref, None, cmd)
            print(f"  + created {ws_ref} {title!r} → {step['session']['sid'][:8]}")
            time.sleep(sleep_send)

        elif kind == "tab-into-pending":
            ws_ref = created.get(step["ws_title"])
            if not ws_ref:
                print(f"  ERROR: no ws ref for {step['ws_title']!r}")
                continue
            ref = _split_and_send(ws_ref, cmd)
            print(f"  + split {ws_ref} {step['ws_title']!r} → {step['session']['sid'][:8]}")
            time.sleep(sleep_split)

        elif kind == "tab-into":
            ref = _split_and_send(step["ws_ref"], cmd)
            print(f"  + split {step['ws_ref']} {step['ws_title']!r} → {step['session']['sid'][:8]}")
            time.sleep(sleep_split)


def _send(ws_ref, surf_ref, cmd):
    args = ["cmux", "send", "--workspace", ws_ref]
    if surf_ref:
        args += ["--surface", surf_ref]
    args.append(cmd)
    subprocess.run(args)
    key_args = ["cmux", "send-key", "--workspace", ws_ref]
    if surf_ref:
        key_args += ["--surface", surf_ref]
    key_args.append("Enter")
    subprocess.run(key_args)


def _split_and_send(ws_ref, cmd):
    """Create a new tab (surface) in ws_ref's current pane and run cmd there."""
    before = _surface_refs(ws_ref)
    subprocess.run(["cmux", "new-surface", "--type", "terminal", "--workspace", ws_ref])
    time.sleep(0.4)
    after = _surface_refs(ws_ref)
    new = [r for r in after if r not in before]
    surf = new[-1] if new else (after[-1] if after else None)
    _send(ws_ref, surf, cmd)
    return surf


def _surface_refs(ws_ref):
    out = subprocess.check_output(
        ["cmux", "rpc", "surface.list", json.dumps({"workspace_id": ws_ref})]
    ).decode()
    try:
        items = json.loads(out)
        if isinstance(items, dict):
            items = items.get("surfaces", items.get("items", []))
        return [s["ref"] for s in items if "ref" in s]
    except json.JSONDecodeError:
        return []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--file", help="Snapshot file (default: ~/.cmux-snapshots/latest.json)")
    ap.add_argument("--since", type=float, default=24.0, help="Only sessions modified within N hours (default 24)")
    ap.add_argument("--min-size", type=int, default=50, help="Skip sessions whose JSONL is smaller than N KB (default 50)")
    ap.add_argument("--workspace", help="Only revive sessions from a specific workspace title (substring)")
    ap.add_argument("--dry-run", action="store_true", help="Preview the plan without executing")
    ap.add_argument("--apply", action="store_true", help="Execute the plan (mutually exclusive with --dry-run)")
    args = ap.parse_args()

    if not args.dry_run and not args.apply:
        ap.error("specify --dry-run or --apply")

    snap_path, snap = load_snapshot(args.file)
    print(f"Snapshot: {snap_path}")
    print(f"Taken:    {snap.get('timestamp', 'unknown')}")

    sessions = extract_sessions(snap)
    enriched = enrich(sessions)

    cutoff = time.time() - args.since * 3600
    candidates = [
        e for e in enriched
        if e["mtime"] >= cutoff and e["size"] >= args.min_size * 1024
        and (not args.workspace or args.workspace.lower() in e["workspace"].lower())
    ]
    candidates.sort(key=lambda e: (e["workspace"], -e["mtime"]))

    print(f"Captured: {len(sessions)}  on-disk: {len(enriched)}  "
          f"matching filters (since {args.since}h, ≥{args.min_size}KB): {len(candidates)}")
    if args.workspace:
        print(f"Workspace filter: {args.workspace!r}")
    print()
    print("--- candidates ---")
    for e in candidates:
        print("  " + label(e))

    if not candidates:
        print("\nNothing to revive.")
        return

    ws_list = live_workspaces()
    plan = build_plan(candidates, ws_list)

    if args.dry_run:
        render_dry_run(plan)
        print("\nDry run only. Re-run with --apply to execute.")
        return

    print("\n--- executing ---")
    execute_plan(plan)
    print("\nDone.")


if __name__ == "__main__":
    main()
