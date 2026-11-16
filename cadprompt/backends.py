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
import urllib.parse
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
    path = _key_file(name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    try:
        # made by hand or brought back by a sync tool: close it for other users
        if os.name == "posix" and os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass
    return text


def _write_key(name, text):
    path = _key_file(name)
    text = (text or "").strip()
    if not text:
        if os.path.exists(path):
            os.remove(path)
        return
    # A new file, then swap: an old file that others could read never holds the
    # new key, and a program that has the old file open does not see it either.
    new = path + ".new"
    if os.path.lexists(new):
        os.remove(new)
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.replace(new, path)


def api_key():
    return _read_key("cadprompt_api_key")


def set_api_key(text):
    _write_key("cadprompt_api_key", text)


def local_key():
    return _read_key("cadprompt_local_key")


def set_local_key(text):
    _write_key("cadprompt_local_key", text)


def same_server(a, b):
    """True when two addresses name the same server. A key is for one server only."""
    def origin(url):
        url = (url or "").strip()
        if "://" not in url:
            url = "//" + url     # no scheme typed: without this every such address has no host at all
        try:
            u = urllib.parse.urlsplit(url)
        except ValueError:       # half typed, for example "http://[": not a server yet
            return None
        return u.scheme.lower(), u.netloc.lower()
    a, b = origin(a), origin(b)
    return a is not None and a == b


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

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The key goes only to the address the user typed, never to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def list_models(url, key="", timeout=5):
    """Model ids from GET <url>/models. Raises OSError or ValueError."""
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the server address must start with http:// or https://")
    req = urllib.request.Request(url + "/models", headers={"Accept": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as r:
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
