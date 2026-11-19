# SPDX-License-Identifier: MIT
"""The user's yes before data leaves this computer, one yes for each service.

backends.may_contact() asks here before every request of the built-in engine,
before "Find models", and before the Claude Code program is started or asked
for its sign-in. A model server on this computer needs no yes: nothing leaves
the machine. Every other service is asked for once, inside FreeCAD, and the
yes is kept with (kind, host, DATA_KINDS). It is asked again when the host
changes, when DATA_KINDS changes, and when the address of a service that was
allowed with https changes to http (not encrypted: the question says so).

The records are no secrets: they are one FreeCAD parameter ("Consent", JSON).
No Qt in here: the question itself is `asker`, which ask_ui.py sets.
"""

import datetime
import ipaddress
import json
import urllib.parse

# Raise this number when CADprompt starts to send a kind of data that the lists below do
# not name. Every stored yes is then asked for again.
DATA_KINDS = 1

ENGINE = ("openai", "anthropic")       # the built-in engine: CADprompt itself sends the requests
CLAUDE_CODE = ("account", "custom")    # the Claude Code program is started and sends them
PARAM = "Consent"

SENT = ("your messages",
        "the results of the AI's code, and descriptions of the document (object names, sizes)",
        "images that you attach to a message",
        "the text of reference files that the AI reads")
SENT_BY_CLAUDE_CODE = ("your messages",
                       "the results of the AI's code, and descriptions of the document (object names, sizes)",
                       "files of the project folder that the AI reads: images, reference files",
                       "pictures of the 3D view")
PROXY = "A proxy of this computer, when its settings name one, sees the name of the service."
NOT_ENCRYPTED = ("The address starts with http://, so the connection is not encrypted. Other people on "
                 "the same network can read everything that is sent, also the key or the token.")
ONCE = "CADprompt asks once for each service. Settings > Privacy takes your yes back."
ON_THIS_COMPUTER = ("The model server runs on this computer: nothing leaves it, so CADprompt does "
                    "not ask for a permission.")
NOT_NOW = ("Nothing was sent. CADprompt asks again with your next message. To work with another AI, "
           "open Settings and choose it: a local model on this computer needs no permission.")
ALLOW, LATER = "Allow", "Not now"

asker = None      # asker(kind, url) -> bool: the question in FreeCAD's window (ask_ui.ask_consent)


def host_of(url):
    """The host of an address in lower case, or "" for a text that names none."""
    url = (url or "").strip()
    if "://" not in url:
        url = "//" + url
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def on_this_computer(url):
    """True for an address of this computer: localhost, 127.x.x.x, ::1.

    Only the text is looked at, no name is looked up. Every other name counts
    as another computer, also one that the network would send back here.
    """
    host = host_of(url)
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def with_scheme(url, has_key=False):
    """The address as it is used. One that was typed with no scheme gets https when a key
    goes with it, so that a key never crosses a network in clear text by accident; for this
    computer, and with no key, it gets http (a model server in the room has no https)."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = ("http://" if on_this_computer(url) or not has_key else "https://") + url
    return url


def clear_text(url):
    """True for an http:// address of another computer: what is sent crosses the network
    not encrypted."""
    return (url or "").strip().lower().startswith("http://") and not on_this_computer(url)


def needs_none(kind, url):
    """A server on this computer that CADprompt itself talks to: no question.
    Claude Code is always asked for: it is a program of its own, with its own connections."""
    return kind in ENGINE and on_this_computer(url)


# ---- the records ---------------------------------------------------------------
def records():
    """The services with a yes: [{"kind", "host", "data", "when"}], the oldest first.
    "clear": True is there when the yes was given for an address that is not encrypted."""
    from . import paths      # here, not at the top: local_agent uses on_this_computer() without FreeCAD
    try:
        rows = json.loads(paths.params().GetString(PARAM, "") or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("kind"), str)
            and isinstance(r.get("host"), str) and r["host"]] if isinstance(rows, list) else []


def _store(rows):
    from . import paths
    paths.params().SetString(PARAM, json.dumps(rows))


def allowed(kind, url):
    """True when the user said yes to this service, for the data that is sent today."""
    host = host_of(url)
    # a yes for https://host is no yes for http://host: the user was not told "not encrypted"
    clear = clear_text(url)
    return bool(host) and any(r["kind"] == kind and r["host"] == host and r.get("data") == DATA_KINDS
                              and (not clear or r.get("clear") is True) for r in records())


def allow(kind, url):
    host = host_of(url)
    if not host:
        return
    rows = [r for r in records() if (r["kind"], r["host"]) != (kind, host)]
    row = {"kind": kind, "host": host, "data": DATA_KINDS, "when": datetime.date.today().isoformat()}
    if clear_text(url):
        row["clear"] = True
    rows.append(row)
    _store(rows)


def forget(kind, host):
    _store([r for r in records() if (r["kind"], r["host"]) != (kind, host)])


def may_contact(kind, url):
    """True when a request may go to this service now. Asks the user when it must.

    Call it in the GUI thread. "Not now" is not kept: the next request asks again.
    """
    if needs_none(kind, url) or allowed(kind, url):
        return True
    if not host_of(url) or asker is None:
        return False             # no service to name, or nobody to ask: nothing is sent
    if not asker(kind, url):
        return False
    allow(kind, url)
    return True


# ---- the words -----------------------------------------------------------------
def service_name(kind, host):
    """What a service is called in the question and in Settings > Privacy."""
    if kind == "anthropic":
        return "Anthropic (%s), with your API key" % host
    if kind == "account":
        return "Anthropic, through Claude Code with your Claude account"
    if kind == "custom":
        return "%s, through Claude Code" % host
    return "the model server at %s" % host


def question(kind, url):
    """(headline, text) of the question for one service."""
    host = host_of(url)
    here = " (this computer)" if on_this_computer(url) else ""
    bullets = lambda rows: "\n".join("•  " + row for row in rows)
    if kind == "account":
        head = "May CADprompt start Claude Code with your Claude account?"
        body = ("The Claude Code program on this computer is started with your login and sends this data "
                "to Anthropic:\n" + bullets(SENT_BY_CLAUDE_CODE)
                + "\n\nIt is sent each time you send a message, and for each step of the AI. CADprompt also "
                "asks Claude Code whether you are signed in.\n"
                "Claude Code is a program of its own: it can make more connections, as its own settings say.")
    elif kind == "custom":
        head = "May CADprompt start Claude Code for the server at %s%s?" % (host, here)
        body = ("The Claude Code program on this computer is started and sends this data to that server:\n"
                + bullets(SENT_BY_CLAUDE_CODE + ("the endpoint token from Settings",))
                + "\n\nIt is sent each time you send a message, and for each step of the AI.\n"
                "Claude Code is a program of its own: it can make more connections, as its own settings say.")
    elif kind == "anthropic":
        head = "May CADprompt send data to Anthropic (%s)?" % host
        body = ("This service is not on this computer. CADprompt sends to it:\n"
                + bullets(SENT + ("your API key, so that the service knows who asks",))
                + "\n\nIt is sent each time you send a message, and for each step of the AI. "
                "\"Find models\" sends only the key.")
    else:
        head = "May CADprompt send data to the model server at %s?" % host
        body = ("This server is not on this computer. CADprompt sends to it:\n"
                + bullets(SENT + ("the key of this server, when you saved one",))
                + "\n\nIt is sent each time you send a message, and for each step of the AI. "
                "\"Find models\" sends only the key, when you saved one.")
    if kind in ("openai", "custom") and clear_text(url):
        body += "\n" + NOT_ENCRYPTED
    return head, body + "\n" + PROXY + "\n\n" + ONCE
