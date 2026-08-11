#!/usr/bin/env python3
"""Re-apply claude-mem shared-memory setup after a reinstall or plugin update.

Idempotent — safe to run repeatedly. Usage:

    python3 scripts/apply-shared-memory.py [--workspace /path/to/ws ...]

What it restores:

  1. Patches scripts/worker-service.cjs in the plugin cache AND marketplace
     copies so the SessionStart context request omits `platformSource` when
     CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS=true  (shared memory across
     Claude Code / OMP / Cursor / Codex).
  2. Ensures the flag is set in:
       ~/.claude-mem/settings.json      (read by every platform's hooks)
       ~/.claude/settings.json env       (Claude Code hook processes)
  3. (Re)creates Cursor context rules files (.cursor/rules/claude-mem-context.mdc)
     for each workspace and registers them in ~/.claude-mem/cursor-projects.json,
     so Cursor receives the shared context via rules files — Cursor's
     beforeSubmitPrompt hook cannot modify the prompt, so this is the only
     injection path for Cursor.

Default workspaces: /tmp and ~/Desktop/maltpanel. Pass --workspace to add
more. Existing registry entries are preserved.
"""

import argparse
import glob
import json
import os
import sys
import urllib.request
import urllib.parse

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("CLAUDE_MEM_DATA_DIR", os.path.join(HOME, ".claude-mem"))
SETTINGS = os.path.join(DATA_DIR, "settings.json")
CC_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
REGISTRY = os.path.join(DATA_DIR, "cursor-projects.json")
MARKETPLACE = os.path.join(
    HOME, ".claude", "plugins", "marketplaces", "thedotmack", "plugin", "scripts", "worker-service.cjs"
)
CACHE_GLOB = os.path.join(
    HOME, ".claude", "plugins", "cache", "thedotmack", "claude-mem", "*", "scripts", "worker-service.cjs"
)

FLAG = "CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS"

# --- minified-bundle needles (v13.15.0 build shapes) ---------------------
GATE_OLD = 'c=t.platform?`&platformSource=${encodeURIComponent(a)}`:"",'
GATE_NEW = ('c=(di().CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS==="true"||'
            'process.env.CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS==="true")?"":'
            't.platform?`&platformSource=${encodeURIComponent(a)}`:"",')
DEF_OLD = 'CLAUDE_MEM_CONTEXT_SHOW_TERMINAL_OUTPUT:"true",CLAUDE_MEM_WELCOME_HINT_ENABLED:"true",'
DEF_NEW = 'CLAUDE_MEM_CONTEXT_SHOW_TERMINAL_OUTPUT:"true",CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS:"false",CLAUDE_MEM_WELCOME_HINT_ENABLED:"true",'


def patch_bundle(path):
    """Apply both needle edits to a bundle; returns list of applied/reason strings."""
    if not os.path.exists(path):
        return [f"skip (not found): {path}"]
    src = open(path).read()
    out = []
    for old, new, label in ((GATE_OLD, GATE_NEW, "gate"), (DEF_OLD, DEF_NEW, "defaults")):
        n = src.count(old)
        if new in src:
            out.append(f"ok (already patched): {label}")
        elif n == 1:
            open(path, "w").write(src.replace(old, new))
            src = src.replace(old, new)
            out.append(f"patched: {label}")
        elif n == 0:
            out.append(f"WARN: {label} needle not found — bundle version changed?")
        else:
            out.append(f"WARN: {label} needle found {n}x — aborting this file")
            break
    return out


def ensure_flag(path, nested_env=False):
    """Set FLAG=true in a JSON settings file (optionally inside its env block)."""
    if not os.path.exists(path):
        return [f"skip (not found): {path}"]
    data = json.load(open(path))
    target = data.setdefault("env", {}) if nested_env else data
    changed = target.get(FLAG) != "true"
    target[FLAG] = "true"
    json.dump(data, open(path, "w"), indent=2, ensure_ascii=False)
    return [f"{'set' if changed else 'ok (already set)'}: {FLAG}=true in {path}"]


def worker_port():
    return int(os.environ.get("CLAUDE_MEM_WORKER_PORT", 37700))


def fetch_context(project):
    url = f"http://127.0.0.1:{worker_port()}/api/context/inject?project={urllib.parse.quote(project)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.read().decode()


def write_cursor_context(workspace, project, context):
    rules_dir = os.path.join(workspace, ".cursor", "rules")
    os.makedirs(rules_dir, exist_ok=True)
    content = (
        "---\n"
        'alwaysApply: true\n'
        'description: "Claude-mem context from past sessions (auto-updated)"\n'
        "---\n\n"
        "# Memory Context from Past Sessions\n\n"
        "The following context is from claude-mem, a persistent memory system "
        "that tracks your coding sessions.\n\n"
        f"{context}\n"
        "---\n"
        "*Updated after last session. Use claude-mem's MCP search tools for more detailed queries.*\n"
    )
    path = os.path.join(rules_dir, "claude-mem-context.mdc")
    open(path, "w").write(content)
    return path


def register_cursor(workspaces):
    reg = {}
    if os.path.exists(REGISTRY):
        reg = json.load(open(REGISTRY))
    out = []
    for ws in workspaces:
        ws = os.path.expanduser(ws)
        if not os.path.isdir(ws):
            out.append(f"WARN: workspace not a dir, skipping: {ws}")
            continue
        project = os.path.basename(os.path.normpath(ws))
        try:
            ctx = fetch_context(project)
        except Exception as e:  # worker down — still register, skip file
            out.append(f"WARN: worker unreachable for '{project}': {e}")
            continue
        path = write_cursor_context(ws, project, ctx)
        reg.setdefault(project, {"workspacePath": ws, "installedAt": "2026-08-12T00:00:00.000Z"})
        out.append(f"ok: {path} ({len(ctx)} chars, project='{project}')")
    json.dump(reg, open(REGISTRY, "w"), indent=2)
    out.append(f"registry: {REGISTRY} -> {sorted(reg)}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", action="append", default=[])
    args = ap.parse_args()

    workspaces = args.workspace or ["/tmp", os.path.join(HOME, "Desktop", "maltpanel")]
    results = []

    bundles = sorted(glob.glob(CACHE_GLOB)) + [MARKETPLACE]
    for b in bundles:
        results.extend(patch_bundle(b))

    results.extend(ensure_flag(SETTINGS, nested_env=False))
    results.extend(ensure_flag(CC_SETTINGS, nested_env=True))
    results.extend(register_cursor(workspaces))

    print("\n".join(results))
    ok = not any(r.startswith("WARN") for r in results)
    print("\n" + ("OK — shared memory setup is in place." if ok else "DONE with warnings — inspect above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
