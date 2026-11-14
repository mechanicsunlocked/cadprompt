"""The freecad-mcp bridge inside this FreeCAD: localhost, token, running.

Claude Code starts `uvx freecad-mcp`, which talks to this bridge on
127.0.0.1:9875. The bridge runs in this very process, so the panel can set it
up and start it itself, and installs it if it is missing.
"""

import io
import os
import secrets
import shutil
import sys
import tempfile
import urllib.request
import zipfile

import FreeCAD as App

from .paths import FREECAD_MCP_VERSION

REPO_ZIP = "https://github.com/neka-nat/freecad-mcp/archive/refs/tags/v{v}.zip"


def mod_dir():
    return os.path.join(App.getUserAppDataDir(), "Mod")


def _import_rpc():
    """rpc_server from the installed FreeCADMCP addon, or None."""
    try:
        from rpc_server import rpc_server  # on sys.path when FreeCADMCP loaded
        return rpc_server
    except ImportError:
        pass
    addon = os.path.join(mod_dir(), "FreeCADMCP")
    if not os.path.isdir(addon):
        return None
    if addon not in sys.path:
        sys.path.insert(0, addon)
    try:
        from rpc_server import rpc_server
        return rpc_server
    except Exception as e:
        App.Console.PrintError(f"CADprompt: FreeCADMCP addon is broken: {e}\n")
        return None


def installed_version():
    try:
        from rpc_server.version import __version__
        return __version__
    except Exception:
        return None


def install():
    """Download the pinned FreeCADMCP addon from GitHub into Mod/. Returns a message."""
    url = REPO_ZIP.format(v=FREECAD_MCP_VERSION)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    prefix = f"freecad-mcp-{FREECAD_MCP_VERSION}/addon/FreeCADMCP/"
    dest = os.path.join(mod_dir(), "FreeCADMCP")
    if os.path.islink(dest) or os.path.exists(dest):
        raise RuntimeError(f"{dest} already exists; remove it first to reinstall")
    tmp = tempfile.mkdtemp(prefix="cadprompt-")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            members = [m for m in z.namelist() if m.startswith(prefix)]
            if not members:
                raise RuntimeError("the download does not contain addon/FreeCADMCP")
            for m in members:
                rel = m[len(prefix):]
                if not rel or ".." in rel.split("/"):
                    continue
                target = os.path.join(tmp, *rel.split("/"))
                if m.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(m) as src, open(target, "wb") as out:
                        out.write(src.read())
        os.makedirs(mod_dir(), exist_ok=True)
        shutil.move(tmp, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return f"freecad-mcp {FREECAD_MCP_VERSION} installed in {dest}"


def ensure():
    """Make the bridge safe and running. Returns (token, message) or raises."""
    rpc = _import_rpc()
    if rpc is None:
        raise LookupError("The FreeCAD MCP bridge is not installed.")
    from rpc_server.settings import load_settings, save_settings

    s = load_settings()
    changed = False
    wanted = {"remote_enabled": False, "allowed_ips": "127.0.0.1", "auto_start_rpc": True}
    for k, v in wanted.items():
        if s.get(k) != v:
            s[k] = v
            changed = True
    if not s.get("auth_token"):
        s["auth_token"] = secrets.token_urlsafe(32)
        changed = True
    if changed:
        save_settings(s)
        try:
            os.chmod(os.path.join(App.getUserAppDataDir(), "freecad_mcp_settings.json"), 0o600)
        except OSError:
            pass

    running = getattr(rpc, "rpc_server_instance", None) is not None
    if running and changed:
        rpc.stop_rpc_server()       # restart so the token applies
        running = False
    msg = "bridge running"
    if not running:
        msg = rpc.start_rpc_server()
    v = installed_version()
    if v and v != FREECAD_MCP_VERSION:
        msg += f" (addon {v}, expected {FREECAD_MCP_VERSION}: tools may not match)"
    return s["auth_token"], msg
