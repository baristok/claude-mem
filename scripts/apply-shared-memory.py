#!/usr/bin/env python3
"""Re-apply claude-mem shared-memory setup after a reinstall or plugin update.

Idempotent — safe to run repeatedly. Usage:

    python3 scripts/apply-shared-memory.py

What it restores (Claude Code / OMP / Cursor all read one shared memory):

  1. Patches scripts/worker-service.cjs in the plugin cache AND marketplace
     copies so the SessionStart context request omits `platformSource` when
     CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS=true.
  2. Patches the Cursor adapter's formatOutput so the context handler's
     additionalContext is emitted as Cursor's sessionStart `additional_context`
     (Cursor cannot inject via beforeSubmitPrompt; sessionStart is its only
     injection path).
  3. Registers a `sessionStart` hook in ~/.cursor/hooks.json that runs
     `hook cursor context`, and removes the old rules-file approach
     (.cursor/rules/claude-mem-context.mdc + cursor-projects.json) if present.
  4. Ensures the flag is set in:
       ~/.claude-mem/settings.json      (read by every platform's hooks)
       ~/.claude/settings.json env       (Claude Code hook processes)
"""

import argparse
import glob
import json
import os
import sys

HOME = os.path.expanduser("~")
DATA_DIR = os.environ.get("CLAUDE_MEM_DATA_DIR", os.path.join(HOME, ".claude-mem"))
SETTINGS = os.path.join(DATA_DIR, "settings.json")
CC_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CURSOR_HOOKS = os.path.join(HOME, ".cursor", "hooks.json")
CURSOR_REGISTRY = os.path.join(DATA_DIR, "cursor-projects.json")
MARKETPLACE_WS = os.path.join(
    HOME, ".claude", "plugins", "marketplaces", "thedotmack", "plugin", "scripts", "worker-service.cjs"
)
CACHE_GLOB = os.path.join(
    HOME, ".claude", "plugins", "cache", "thedotmack", "claude-mem", "*", "scripts", "worker-service.cjs"
)
BUN = os.environ.get("BUN", os.path.join(HOME, ".bun", "bin", "bun"))

FLAG = "CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS"

# --- minified-bundle needles (v13.15.0 build shapes) ---------------------
GATE_OLD = 'c=t.platform?`&platformSource=${encodeURIComponent(a)}`:"",'
GATE_NEW = ('c=(di().CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS==="true"||'
            'process.env.CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS==="true")?"":'
            't.platform?`&platformSource=${encodeURIComponent(a)}`:"",')
DEF_OLD = 'CLAUDE_MEM_CONTEXT_SHOW_TERMINAL_OUTPUT:"true",CLAUDE_MEM_WELCOME_HINT_ENABLED:"true",'
DEF_NEW = 'CLAUDE_MEM_CONTEXT_SHOW_TERMINAL_OUTPUT:"true",CLAUDE_MEM_CONTEXT_SHARE_ALL_PLATFORMS:"false",CLAUDE_MEM_WELCOME_HINT_ENABLED:"true",'
CURSOR_FO_OLD = "edits:e.edits}},formatOutput(t){return{continue:t.continue??!0}}"
CURSOR_FO_NEW = ("edits:e.edits}},formatOutput(t){let e=t.hookSpecificOutput?.additionalContext;"
                 "return e?{continue:!0,additional_context:e}:{continue:t.continue??!0}}")

PATCHES = [
    (GATE_OLD, GATE_NEW, "platformSource gate"),
    (DEF_OLD, DEF_NEW, "DEFAULTS entry"),
    (CURSOR_FO_OLD, CURSOR_FO_NEW, "cursor additional_context passthrough"),
]


def patch_bundle(path):
    """Apply all needle edits to a bundle; returns list of applied/reason strings."""
    if not os.path.exists(path):
        return [f"skip (not found): {path}"]
    src = open(path).read()
    out = []
    for old, new, label in PATCHES:
        n = src.count(old)
        if new in src:
            out.append(f"ok (already patched): {label}")
        elif n == 1:
            src = src.replace(old, new)
            out.append(f"patched: {label}")
        elif n == 0:
            out.append(f"WARN: {label} needle not found — bundle version changed?")
        else:
            out.append(f"WARN: {label} needle found {n}x — aborting this file")
            break
    open(path, "w").write(src)
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


def ensure_cursor_sessionstart():
    """Register the sessionStart hook in ~/.cursor/hooks.json; drop rules approach."""
    out = []
    if not os.path.exists(MARKETPLACE_WS):
        out.append("WARN: marketplace worker-service.cjs not found — cannot wire sessionStart hook")
        return out

    cmd = f'"{BUN}" "{MARKETPLACE_WS}" hook cursor context'
    hooks = {}
    if os.path.exists(CURSOR_HOOKS):
        hooks = json.load(open(CURSOR_HOOKS))

    existing = (hooks.get("hooks") or {}).get("sessionStart") or []
    if any(e.get("command") == cmd for e in existing):
        out.append("ok (already registered): sessionStart hook in ~/.cursor/hooks.json")
    else:
        hooks.setdefault("hooks", {}).setdefault("sessionStart", []).append({"command": cmd})
        json.dump(hooks, open(CURSOR_HOOKS, "w"), indent=2)
        out.append("registered: sessionStart -> `hook cursor context` in ~/.cursor/hooks.json")

    # remove the abandoned rules-file approach if present
    for f in (CURSOR_REGISTRY,):
        if os.path.exists(f):
            os.remove(f)
            out.append(f"removed (rules approach abandoned): {f}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()

    results = []
    bundles = sorted(glob.glob(CACHE_GLOB)) + [MARKETPLACE_WS]
    for b in bundles:
        results.extend(patch_bundle(b))

    results.extend(ensure_flag(SETTINGS, nested_env=False))
    results.extend(ensure_flag(CC_SETTINGS, nested_env=True))
    results.extend(ensure_cursor_sessionstart())

    print("\n".join(results))
    ok = not any(r.startswith("WARN") for r in results)
    print("\n" + ("OK — shared memory setup is in place." if ok else "DONE with warnings — inspect above."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
