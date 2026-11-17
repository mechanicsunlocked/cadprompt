"""Which AI does the work, and what it needs to start.

Four sources, chosen in Settings (parameter "Backend"):
  account  the user's Claude account, through Claude Code
  apikey   an Anthropic API key, through the built-in engine
  local    a local or other OpenAI-compatible server, through the built-in engine
  custom   Claude Code pointed at another Anthropic-compatible endpoint

No Qt in here, so the settings and the tests can use it anywhere.
"""

import json
import os
import stat
import urllib.parse
import urllib.request

import FreeCAD as App

from . import paths

BACKENDS = ("account", "apikey", "local", "custom")
LABELS = {"account": "Claude account", "apikey": "Anthropic API key",
          "local": "Local model", "custom": "Other endpoint"}
LOCAL_URL = "http://127.0.0.1:1234/v1"   # LM Studio's default; Ollama is http://127.0.0.1:11434/v1
ANTHROPIC_URL = "https://api.anthropic.com"   # fixed: the saved API key goes to this service only
ANTHROPIC_VERSION = "2026-10-21"         # the same API version as local_agent.AnthropicWire
API_MODEL = "claude-opus-5-5"            # used until the user picks another one in Settings
API_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")   # offered before Find models
ENGINE = ("apikey", "local")             # the sources of the built-in engine: no Claude Code


def current():
    b = paths.params().GetString("Backend", "account")
    return b if b in BACKENDS else "account"


def uses_claude_code(backend=None):
    return (backend or current()) not in ENGINE


def may_contact(kind, url):
    """True when CADprompt may send a request to this service now.

    The one gate for everything the addon itself sends to an AI service:
    kind is "openai" or "anthropic", url the address of the service. The
    built-in engine asks before every request (local_engine: in the GUI
    thread, also for requests that its worker thread sends), and Settings asks
    before "Find models". On False nothing is sent and no connection is
    opened. A consent question of the user belongs here.
    """
    return True


def label(backend=None):
    return LABELS.get(backend or current(), LABELS["account"])


# ---- keys -------------------------------------------------------------------
# Keys live in their own files, not in FreeCAD's parameter file (user.cfg):
# that one is plain text that people share when they ask for help.
# Linux and macOS: the file has mode 0600, only the user can read it.
# Windows has no such modes. There the file is in FreeCAD's folder inside the
# user's own profile (AppData), and the permissions of that folder keep other
# users out. That is the same protection as for the user's other files.

POSIX = os.name == "posix"


def _key_file(name):
    return os.path.join(App.getUserAppDataDir(), name)


def _read_key(name):
    path = _key_file(name)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except (OSError, ValueError):        # no file, or not text
        return ""
    try:
        # made by hand or brought back by a sync tool: close it for other users
        if POSIX and os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass
    return text


def _writable(path):
    """Windows refuses to replace or delete a file that is marked read-only."""
    if not POSIX:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass


def _write_key(name, text):
    path = _key_file(name)
    text = (text or "").strip()
    if not text:
        if os.path.exists(path):
            _writable(path)
            os.remove(path)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A new file, then swap: an old file that others could read never holds the
    # new key, and a program that has the old file open does not see it either.
    new = path + ".new"
    if os.path.lexists(new):
        _writable(new)
        os.remove(new)
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    _writable(path)
    try:
        os.replace(new, path)
    except PermissionError:
        if POSIX:
            raise
        # Windows does not swap a file that a program has open (a virus scanner,
        # a sync tool). Writing into the old file is allowed, and no mode is lost.
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        finally:
            os.remove(new)


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


def api_model():
    return paths.params().GetString("ApiModel", "").strip() or API_MODEL


def engine_config(backend=None):
    """What the built-in engine needs for a source: {"kind", "url", "model", "key"}.

    kind is the wire format, "openai" or "anthropic". Each key goes with its
    own service only: the Anthropic key to ANTHROPIC_URL, the local key to
    the local server. None for the sources that run Claude Code.
    """
    b = backend or current()
    if b == "apikey":
        return {"kind": "anthropic", "url": ANTHROPIC_URL, "model": api_model(), "key": api_key()}
    if b == "local":
        return dict(local_config(), kind="openai")
    return None


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
    if not uses_claude_code(b):
        return {}, []        # the built-in engine does the work, not Claude Code
    if b == "custom":
        return custom_env(), ["ANTHROPIC_API_KEY"]
    return {}, ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]


# ---- asking a service for its models ---------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The key goes only to the address the user typed, never to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def list_models(url, key="", timeout=5, kind="openai"):
    """Model ids from GET <url>/models (kind "anthropic": <url>/v1/models).

    Raises OSError or ValueError. The caller asks may_contact() first.
    """
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the server address must start with http:// or https://")
    if kind == "anthropic":
        req = urllib.request.Request(url + "/v1/models?limit=100", headers={
            "Accept": "application/json", "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})
    else:
        req = urllib.request.Request(url + "/models", headers={"Accept": "application/json"})
        if key:
            req.add_header("Authorization", "Bearer " + key)
    # No proxy, like the engine: urllib would hand the request to the proxy of the system
    # settings, and the key of an http server would go there in clear text.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(req, timeout=timeout) as r:
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
