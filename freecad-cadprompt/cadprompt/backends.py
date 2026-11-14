"""Which AI does the work, and what it needs to start.

Four sources, chosen in Settings (parameter "Backend"):
  account  the user's Claude account, through Claude Code
  apikey   an Anthropic API key, through Claude Code
  local    a local or other OpenAI-compatible server, through the built-in engine
  custom   Claude Code pointed at another Anthropic-compatible endpoint

No Qt in here, so the settings and the tests can use it anywhere.
"""

import json
import os
import urllib.request

import FreeCAD as App

from . import paths

BACKENDS = ("account", "apikey", "local", "custom")
LABELS = {"account": "Claude account", "apikey": "Anthropic API key",
          "local": "Local model", "custom": "Other endpoint"}
LOCAL_URL = "http://127.0.0.1:1234/v1"   # LM Studio's default; Ollama is http://127.0.0.1:11434/v1


def current():
    b = paths.params().GetString("Backend", "account")
    return b if b in BACKENDS else "account"


def uses_claude_code(backend=None):
    return (backend or current()) != "local"


def label(backend=None):
    return LABELS.get(backend or current(), LABELS["account"])


# ---- keys -------------------------------------------------------------------
# Keys live in their own files that only the user can read. FreeCAD's parameter
# file (user.cfg) is plain text that people share when they ask for help.

def _key_file(name):
    return os.path.join(App.getUserAppDataDir(), name)


def _read_key(name):
    try:
        with open(_key_file(name), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_key(name, text):
    path = _key_file(name)
    text = (text or "").strip()
    if not text:
        if os.path.exists(path):
            os.remove(path)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.chmod(path, 0o600)    # the mode above does not apply to a file that was there before


def api_key():
    return _read_key("cadprompt_api_key")


def set_api_key(text):
    _write_key("cadprompt_api_key", text)


def local_key():
    return _read_key("cadprompt_local_key")


def set_local_key(text):
    _write_key("cadprompt_local_key", text)


def local_config():
    p = paths.params()
    return {"url": p.GetString("LocalUrl", LOCAL_URL).strip().rstrip("/"),
            "model": p.GetString("LocalModel", "").strip(),
            "key": local_key()}


# ---- Claude Code ------------------------------------------------------------

def custom_env():
    """Environment for Claude Code on another endpoint, or {} when that is not chosen."""
    p = paths.params()
    if p.GetString("Backend", "account") != "custom":
        return {}
    url = p.GetString("BaseUrl", "")
    if not url:
        return {}
    env = {"ANTHROPIC_BASE_URL": url,
           "ANTHROPIC_AUTH_TOKEN": p.GetString("AuthToken", "") or "local"}
    model = p.GetString("Model", "")
    if model:
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[k] = model
    return env


def claude_env():
    """(set, unset): changes to the environment of the Claude Code process.

    The unset list matters as much as the set one: a key or an endpoint left
    in the user's shell would otherwise win over the source chosen here, and
    bill the API when the user picked the subscription.
    """
    b = current()
    if b == "apikey":
        return {"ANTHROPIC_API_KEY": api_key()}, ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    if b == "custom":
        return custom_env(), ["ANTHROPIC_API_KEY"]
    if b == "local":
        return {}, []        # the built-in engine does the work, not Claude Code
    return {}, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]


# ---- local server -----------------------------------------------------------

def list_models(url, key="", timeout=5):
    """Model ids from GET <url>/models. Raises OSError or ValueError."""
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the server address must start with http:// or https://")
    req = urllib.request.Request(url + "/models", headers={"Accept": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read(4_000_000).decode("utf-8", "replace"))
    rows = data.get("data", data.get("models")) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("the server did not answer with a list of models")
    ids = []
    for row in rows:
        mid = (row.get("id") or row.get("name")) if isinstance(row, dict) else row
        if isinstance(mid, str) and mid and mid not in ids:
            ids.append(mid)
    return ids
