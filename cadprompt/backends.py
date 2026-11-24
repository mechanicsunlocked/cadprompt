"""Which AI does the work, and what it needs to start.

Four sources, chosen in Settings (parameter "Backend"):
  account  the user's Claude account, through Claude Code
  apikey   an Anthropic API key, through the built-in engine
  local    a local or other OpenAI-compatible server, through the built-in engine
  custom   Claude Code pointed at another Anthropic-compatible endpoint
A new installation has none of them: the user chooses first (current() is "").

No Qt in here, so the settings and the tests can use it anywhere.
"""

import http.client
import json
import os
import stat
import urllib.error
import urllib.parse

import FreeCAD as App

from . import consent, paths

BACKENDS = ("account", "apikey", "local", "custom")
LABELS = {"account": "Claude account", "apikey": "Anthropic API key",
          "local": "Local model", "custom": "Other endpoint"}
LOCAL_URL = "http://127.0.0.1:1234/v1"   # LM Studio's default; Ollama is http://127.0.0.1:11434/v1
ANTHROPIC_URL = "https://api.anthropic.com"   # fixed: the saved API key goes to this service only
ANTHROPIC_VERSION = "2026-10-21"         # the same API version as local_agent.AnthropicWire
API_MODEL = "claude-opus-5-5"            # used until the user picks another one in Settings
API_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")   # offered before Find models
ENGINE = ("apikey", "local")             # the sources of the built-in engine: no Claude Code
CLAUDE_CODE = ("account", "custom")      # the sources that start the Claude Code program
NO_SOURCE = "No AI chosen"


def current():
    """The source the user chose, or "" when none is chosen yet: there is no default service."""
    b = paths.params().GetString("Backend", "")
    return b if b in BACKENDS else ""


def uses_claude_code(backend=None):
    return (backend or current()) in CLAUDE_CODE


def service(backend=None):
    """(kind, url) of the service that a source sends data to, as may_contact() wants it.
    None when no source is chosen."""
    b = backend or current()
    if b == "apikey":
        return "anthropic", ANTHROPIC_URL
    if b == "local":
        return "openai", local_config()["url"]
    url = paths.params().GetString("BaseUrl", "").strip()
    if b == "custom" and url:
        return "custom", url
    if b in CLAUDE_CODE:                     # no endpoint set: Claude Code works with the account
        return "account", ANTHROPIC_URL      # where Claude Code sends the conversation
    return None


def may_contact(kind, url):
    """True when CADprompt may send a request to this service now.

    The one gate for everything that leaves the computer for an AI service.
    kind "openai" or "anthropic": a request of the addon itself to the service
    at `url`. The built-in engine asks before every request (local_engine: in
    the GUI thread, also for requests that its worker thread sends), and
    Settings asks before "Find models". kind "account" or "custom": the panel
    asks before it starts the Claude Code program or asks it for the sign-in.
    On False nothing is sent, no connection is opened and no program starts.

    A server on this computer needs no yes. Every other service is asked for
    once, in FreeCAD's window (consent.py). Call it in the GUI thread.
    """
    return consent.may_contact(kind, url)


def label(backend=None):
    return LABELS.get(backend or current(), NO_SOURCE)


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


def endpoint_token():
    """The token of "Claude Code with another endpoint"."""
    p = paths.params()
    old = p.GetString("AuthToken", "").strip()
    if old:
        # The first versions kept it in user.cfg. The first read moves it to its own file.
        try:
            _write_key("cadprompt_endpoint_token", old)
        except OSError:
            return old               # no file can be written: the token still works
        p.RemString("AuthToken")
        App.saveParameter()          # out of user.cfg now, not when FreeCAD closes
    return _read_key("cadprompt_endpoint_token")


def set_endpoint_token(text):
    _write_key("cadprompt_endpoint_token", text)
    p = paths.params()
    if p.GetString("AuthToken", ""):
        p.RemString("AuthToken")
        App.saveParameter()


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
    key = local_key()
    # with its scheme, as the engine uses it: the consent question names the same address
    return {"url": consent.with_scheme(p.GetString("LocalUrl", LOCAL_URL).strip().rstrip("/"), bool(key)),
            "model": p.GetString("LocalModel", "").strip(),
            "key": key}


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
    if current() != "custom":
        return {}
    url = p.GetString("BaseUrl", "").strip()
    if not url:
        return {}
    env = {"ANTHROPIC_BASE_URL": url,
           "ANTHROPIC_AUTH_TOKEN": endpoint_token() or "local"}
    model = p.GetString("Model", "")
    if model:
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                  "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            env[k] = model
    return env


# Variables of the user's shell that send Claude Code to another service than the one the
# user said yes to, or give it a key or a login for one (read in the text of Claude Code
# 2.1.327): CLAUDE_CODE_USE_BEDROCK, _VERTEX, _FOUNDRY, _MANTLE, _GATEWAY and more choose
# another provider, which wins over the login and over ANTHROPIC_BASE_URL.
# ANTHROPIC_<provider>_BASE_URL, ANTHROPIC_UNIX_SOCKET and CLAUDE_CODE_API_BASE_URL are other
# addresses. ANTHROPIC_<provider>_API_KEY, ANTHROPIC_PROFILE and ANTHROPIC_CUSTOM_HEADERS are
# keys, logins and headers. Whole families and not single names: each version of Claude Code
# has more of them.
SHELL_FAMILIES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
SHELL_NAMES = ("CLAUDE_CODE_API_BASE_URL", "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL")
# These only choose a model; the user's choice in the shell stays.
MODEL_CHOICE = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_",
                "ANTHROPIC_CUSTOM_MODEL_OPTION")


def _other_service(name):
    """True for a variable that can send Claude Code somewhere else, or give it a key."""
    name = name.upper()      # Windows: the names have no case
    return name in SHELL_NAMES or (name.startswith(SHELL_FAMILIES) and not name.startswith(MODEL_CHOICE))


def claude_env(names=()):
    """(set, unset): changes to the environment of the Claude Code process.

    `names`: the variables that the process would start with.
    The unset list matters as much as the set one: a key, an endpoint or a
    provider switch left in the user's shell would otherwise win over the
    source chosen here. The data would go to a service that the user was not
    asked for, or bill the API when the user picked the subscription.
    """
    b = current()
    if not uses_claude_code(b):
        return {}, []        # the built-in engine does the work, not Claude Code
    add = custom_env() if b == "custom" else {}
    # No address (the account, and "another endpoint" with none): Claude Code works with its
    # login. An endpoint of the user's shell must not get the data that the user allowed for
    # Anthropic (service() names the account then, and that is what the user was asked for).
    drop = ["ANTHROPIC_API_KEY"] if add else ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    return add, drop + [n for n in names if _other_service(n) and n not in drop]


# ---- asking a service for its models ---------------------------------------

def list_models(url, key="", timeout=5, kind="openai", connect_timeout=None):
    """Model ids from GET <url>/models (kind "anthropic": <url>/v1/models).

    `timeout` is for the answer; reaching the server has its own short limit
    (local_agent.connect). Raises OSError (urllib.error.HTTPError for an
    answer that is not 200) or ValueError. The caller asks may_contact() first.
    """
    url = (url or "").strip().rstrip("/")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("the server address must start with http:// or https://")
    if kind == "anthropic":
        address = url + "/v1/models?limit=100"
        headers = {"Accept": "application/json", "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    else:
        address = url + "/models"
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
    # The same way as the engine (local_agent.connect): a proxy only as a tunnel for https, never
    # for http (the key of an http server would go to the proxy in clear text), and no redirect
    # (the key goes only to the address the user typed).
    from . import local_agent        # here, not at the top: the panel loads this module when it opens
    u = urllib.parse.urlsplit(address)
    conn = local_agent.connect(address, timeout, connect_timeout)
    try:
        conn.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""), headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            raise urllib.error.HTTPError(address, resp.status, resp.reason, resp.headers, None)
        raw = resp.read(4_000_000)
    except http.client.HTTPException as exc:
        raise OSError("the answer of the server is not HTTP: %s" % exc.__class__.__name__)
    finally:
        conn.close()
    data = json.loads(raw.decode("utf-8", "replace"))
    rows = data.get("data", data.get("models")) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("the server did not answer with a list of models")
    ids = []
    for row in rows:
        mid = (row.get("id") or row.get("name")) if isinstance(row, dict) else row
        if isinstance(mid, str) and mid and mid not in ids:
            ids.append(mid)
    return ids
