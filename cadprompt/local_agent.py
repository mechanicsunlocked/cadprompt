"""The chat loop of the built-in engine: a model with tool calls, over HTTP.

Pure Python (no Qt, no FreeCAD), so it also runs in plain python3 tests.
The loop sends the conversation to a model service, runs the tools the model
asks for through `run_tool`, and goes on until the model answers without a
tool call. It is written for small models: they forget to check their work,
give empty answers and have a short context, so the loop checks, nudges and
shortens for them.

Two wire formats, one loop. OpenAIWire speaks chat completions (LM Studio,
Ollama, llama.cpp, or a provider with a key). AnthropicWire speaks the
Anthropic Messages API. The conversation itself has one neutral shape, so a
project can change its service in the middle:

    {"role": "user", "content": text, "images": [paths in the project]}
    {"role": "assistant", "content": text, "tool_calls": [{"id", "type": "function",
                                    "function": {"name", "arguments": JSON text}}]}
    {"role": "tool", "tool_call_id": id, "content": text}

"images" and "tool_calls" are left out when empty. A user message can have
"shown" (what the user typed, when the content is an expanded command) and
"auto" (written by the loop, not by the user). Keys that start with "_" live
only in memory during one request and are never stored.
"""

import http.client
import ipaddress
import json
import re
import socket
import threading
import urllib.parse
import urllib.request

from . import main_thread

MARK = "\n... (shortened, %d characters in total)"

CHECK = ("Automatic check: the work is not complete. Your last tool result had this problem:\n%s\n"
         "Correct it with the tools. Delete objects that you made and that are broken or not "
         "necessary. Then check with get_document_state before you answer. If you cannot correct "
         "it, say clearly what does not work.")
NUDGE = ("Your last answer was empty. Continue: use the tools to complete the work, or answer "
         "with a short summary of what you did.")
NO_ANSWER = "The model gave no final answer. Look at the result in FreeCAD, or ask again."
STILL_WRONG = ("*Automatic check: the last tool result still shows a problem. "
               "Look at the model before you use it.*")
STEP_LIMIT = "I stopped because the step limit (%d) was reached. The work can be incomplete."
SAME_CALL = ("ERROR: this is exactly the same call as the last one, which failed. It was not run "
             "again. The error was:\n%s\nChange the code before you call the tool again.")
CUT_OFF = "*The answer was cut off because it became too long. Ask for the rest, or for smaller steps.*"
NO_VISION = ("This model cannot read images. Send your message again and it goes without the "
             "images, or choose a model that can read images in Settings.")
NO_VISION_NOTE = "This model cannot read images. The images of earlier messages are left out."
NOT_ALLOWED = "Nothing was sent: CADprompt is not allowed to contact %s."
BAD_PICTURE = "The image %s could not be read. Add it again, or send the message without it."
PICTURE_GONE = "(An image was attached here: %s. The file is not there any more.)"
PICTURE_LEFT_OUT = "(An image was attached here: %s. It is not sent again.)"

SENT_KEYS = ("role", "content", "tool_calls", "tool_call_id")   # what counts for the size of a message
VIEW_KEYS = SENT_KEYS + ("images", "_native")   # what a wire format gets to build its request from
REPAIR = ("run_python", "get_document_state")    # only their results show the state of the model
MAX_IMAGES = 4                                   # pictures with one message
RETRIES = 3                                      # waits for a busy service before the run gives up
BACKOFF = 2                                      # seconds of the first wait when the service names none
LONGEST_WAIT = 60                                # seconds; a longer wait is the user's decision
CONNECT_TIMEOUT = 15                             # seconds to reach the server; an answer can take minutes

_REFUSED = {}     # (address, model) -> request fields that server does not take


class Stopped(Exception):
    """The user stopped the run."""


class AgentError(Exception):
    """The run cannot go on. The text is written for the user."""


class NoVision(AgentError):
    """The model cannot read the pictures of this request."""


class _Overflow(Exception):
    """The server says the conversation does not fit the model's context."""


class _Refused(Exception):
    """The server does not take one of the optional request fields."""


class _Busy(Exception):
    """The service asks to wait: `seconds` (None: it did not say), `note` while waiting, `text` to give up."""

    def __init__(self, seconds, note, text):
        super().__init__(text)
        self.seconds, self.note, self.text = seconds, note, text


class _NoPictures(Exception):
    """The server does not take picture input."""


class _BadThinking(Exception):
    """The service does not take back the thinking blocks of an earlier step."""


class NoConnection(OSError):
    """The server was not reached in time. Not a TimeoutError: that one means a slow answer."""


def connect(address, timeout, connect_timeout=None):
    """An open http.client connection to the server of `address`.

    Two time limits: a short one to reach the server, so that a network which
    drops the packets gives its error soon, and `timeout` for each step of
    the answer, which a model can take minutes for. No proxy: http.client
    connects to the address itself. Raises OSError.
    """
    main_thread.quiet_pipe()     # a server that closes the line in the middle must not end FreeCAD
    u = urllib.parse.urlsplit(address)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    short = CONNECT_TIMEOUT if connect_timeout is None else connect_timeout
    conn = cls(u.hostname or "", u.port, timeout=min(short, timeout))
    try:
        conn.connect()
    except TimeoutError:
        conn.close()
        raise NoConnection("no connection within %g seconds" % min(short, timeout))
    except BaseException:
        conn.close()
        raise
    main_thread.quiet_socket(conn.sock)
    conn.timeout = timeout
    conn.sock.settimeout(timeout)
    return conn


def user_message(text, shown=None, images=None):
    """A request of the user. `shown` is what they typed when `text` is an expanded command.
    `images` are paths of pictures in the project that go with it."""
    msg = {"role": "user", "content": text}
    if shown and shown != text:
        msg["shown"] = shown
    if images:
        msg["images"] = list(images)
    return msg


def public(messages):
    """The messages as they are stored: without the keys that live in memory only."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def find_problem(name, result):
    """The part of a tool result that the model must not ignore, or ''.

    Only the tool's own lines count. What the model's code printed is
    indented by run_python, so a printed "ERROR margin 0.2" is not a failure.
    """
    bad = result.startswith("ERROR")
    if not bad and name in REPAIR:
        bad = any(line == "ERROR:" or line.startswith("PROBLEM OBJECTS: ")
                  for line in result.splitlines())
    return result[-1500:] if bad else ""


def _visible(content):
    """The answer text without the model's thinking."""
    if isinstance(content, list):      # some servers answer with a list of parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return ""
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def _cut(text, limit):
    return text if len(text) <= limit + 60 else text[:limit] + MARK % len(text)


def _size(messages, image_chars=0):
    text = len(json.dumps([{k: m[k] for k in SENT_KEYS if k in m} for m in messages]))
    return text + image_chars * sum(len(m.get("images") or ()) for m in messages)


def _shorten(messages, keep, limit):
    """Cut the tool results, and the code that made them, of all but the last `keep` results."""
    tools = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    old = set(tools[:-keep] if keep else tools)
    out = []
    for i, m in enumerate(messages):
        if i in old:
            m = dict(m, content=_cut(m.get("content") or "", limit))
        elif m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                j += 1
            if all(k in old for k in range(i + 1, j)):
                m = dict(m, tool_calls=[_short_call(c, limit) for c in m["tool_calls"]])
                m.pop("_native", None)       # the service's own copy of this step no longer fits
        out.append(m)
    return out


def _short_call(call, limit):
    fn = call.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except ValueError:
        args = None
    if not isinstance(args, dict):
        return call
    args = {k: _cut(v, limit) if isinstance(v, str) else v for k, v in args.items()}
    return dict(call, function=dict(fn, arguments=json.dumps(args)))


def _add_line(text, line):
    return ((text or "") + "\n\n" + line).strip()


def _no_images(m):
    """The message with one line of text in place of its pictures."""
    out = {k: v for k, v in m.items() if k != "images"}
    out["content"] = _add_line(m.get("content"), PICTURE_LEFT_OUT % ", ".join(m["images"]))
    return out


def build_view(messages, budget, image_chars=0, images=True):
    """What is sent of the stored conversation: (messages, something_left_out).

    The stored list stays complete. When it is larger than `budget` characters:
    the pictures of old messages go first (a picture counts as `image_chars`),
    then old tool results and old code are cut, then whole old requests are
    left out, then the current request is cut harder. `images=False` sends no
    picture at all, for a model that cannot read them.
    """
    msgs, dropped = list(messages), False
    pictures = [i for i, m in enumerate(msgs) if m.get("images")]
    if images and _size(msgs, image_chars) > budget:
        pictures = pictures[:-1]          # the newest pictures stay: the user is asking about them
    elif images:
        pictures = []
    for i in pictures:
        msgs[i] = _no_images(msgs[i])
    if _size(msgs, image_chars) > budget:
        msgs = _shorten(msgs, 2, 300)
    if _size(msgs, image_chars) > budget:
        starts = [i for i, m in enumerate(msgs) if m.get("role") == "user" and not m.get("auto")]
        for start in starts[1:]:
            dropped = True
            if _size(msgs[start:], image_chars) <= budget or start == starts[-1]:
                msgs = msgs[start:]
                break
    if _size(msgs, image_chars) > budget:
        msgs = _shorten(msgs, 1, 150)
        last = max((i for i, m in enumerate(msgs) if m.get("role") == "tool"), default=None)
        if last is not None:
            msgs[last] = dict(msgs[last], content=_cut(msgs[last].get("content") or "", 2000))
    out = []
    for m in msgs:
        m = {k: m[k] for k in VIEW_KEYS if k in m}
        if m["role"] == "user" and out and out[-1]["role"] == "user":
            # some chat templates refuse two user messages in a row
            both = (out[-1].get("images") or []) + (m.get("images") or [])
            out[-1] = dict(out[-1], content=out[-1]["content"] + "\n\n" + m["content"])
            if both:
                out[-1]["images"] = both
        else:
            out.append(m)
    return out, dropped


# ---- the wire formats ----------------------------------------------------------
# A wire format turns the neutral conversation into one HTTP request and the
# HTTP answer back into one neutral assistant message:
#   request(system, view, tools, max_tokens, temperature, picture) -> (address, headers, body)
#   answer(status, headers, raw, pictures) -> message, or one of the exceptions above
#   unreachable(exc, timeout) -> the text for "no connection"
# `picture(path)` gives (media type, base64 text) of a picture, or None when
# the file cannot be read any more. Its numbers (max_tokens, context_chars,
# max_steps, image_chars) are the defaults of an Agent with this format.

def _detail(raw):
    """(the JSON of an answer or None, the "error" entry, its text in one line)."""
    text = raw.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        detail = str(error.get("message") or error.get("code") or error)
    else:
        detail = str(error or text)
    return data, error, " ".join(detail.split())[:300]


def _retry_after(headers):
    try:
        return max(0.0, float(headers.get("retry-after")))
    except (TypeError, ValueError):
        return None        # no header, or a date: use our own steps


class OpenAIWire:
    """Chat completions, as LM Studio, Ollama, llama.cpp and many providers speak them."""

    kind = "openai"
    max_tokens, context_chars, max_steps = 6000, 12000, 20   # about 3 characters per token: room in 8k
    image_chars = 1000              # a small model reads a picture as a few hundred tokens
    too_long = ("The conversation is too long for this model. Start a new chat, "
                "or load the model with a larger context length.")

    def __init__(self, url, model, key):
        self.key = key
        url = (url or "").strip().rstrip("/")
        if url and "://" not in url:
            # no scheme typed: a key must never go out in clear text, so only this computer gets http
            host = urllib.parse.urlsplit("//" + url).hostname or ""
            try:
                here = ipaddress.ip_address(host).is_loopback
            except ValueError:
                here = host == "localhost"
            url = ("http://" if here or not key else "https://") + url
        self.url, self.model = url, model
        self.where = "the model server at %s" % url
        # request fields this server does not take; remembered for the next request to it
        self.refused = _REFUSED.setdefault((url, model), set())

    def request(self, system, view, tools, max_tokens, temperature, picture):
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system}] + [self._message(m, picture) for m in view]}
        # OpenAI's newer models take only the new name of the limit, and no temperature
        limit = "max_completion_tokens" if "max_tokens" in self.refused else "max_tokens"
        body[limit] = max_tokens
        if "temperature" not in self.refused:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        return self.url + "/chat/completions", headers, body

    def _message(self, m, picture):
        out = {k: m[k] for k in SENT_KEYS if k in m}
        if m.get("images"):
            got = [(path, picture(path)) for path in m["images"]]
            text = out.get("content") or ""
            gone = [path for path, data in got if not data]
            if gone:
                text = _add_line(text, PICTURE_GONE % ", ".join(gone))
            parts = [{"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % data}}
                     for _, data in got if data]
            out["content"] = [{"type": "text", "text": text}] + parts if parts else text
        return out

    def answer(self, status, headers, raw, pictures):
        data, error, detail = _detail(raw)
        if status == 200 and not error:
            try:
                choice = data["choices"][0]
                msg = choice["message"]
                if isinstance(msg, dict):
                    text = _visible(msg.get("content"))
                    if choice.get("finish_reason") == "length" and text and not msg.get("tool_calls"):
                        # the text stops in the middle (a cut tool call fails by itself: no valid JSON)
                        msg = dict(msg, content=_add_line(text, CUT_OFF), _cut_off=True)
                    return msg
            except (KeyError, IndexError, TypeError, AttributeError):
                pass
            raise AgentError("The answer of the server at %s was not understood. Is it an "
                             "OpenAI-compatible server? The address usually ends in /v1." % self.url)
        low = detail.lower()
        if status in (401, 403):
            raise AgentError("The server did not accept the API key (HTTP %d). Check the key in "
                             "Settings." % status)
        if status == 429:
            raise _Busy(_retry_after(headers),
                        "The model server asks to wait %d seconds (too many requests). Waiting…",
                        "The model server takes no more requests at the moment (HTTP 429): %s. "
                        "Wait a minute and ask again." % (detail or "no text"))
        if pictures and any(w in low for w in ("image", "vision", "multimodal", "multi-modal", "mmproj",
                                               "must be a string", "expected a string")):
            raise _NoPictures()
        if ("context" in low and any(w in low for w in ("length", "window", "exceed", "overflow", "size"))) \
                or "too many tokens" in low or "too long" in low:
            raise _Overflow()
        if status == 400 and ("unsupported" in low or "not supported" in low):
            for field in ("max_tokens", "temperature"):
                if field in low and field not in self.refused:     # once per field, so no endless loop
                    raise _Refused(field)
        if "model" in low and any(w in low for w in ("not found", "not loaded", "does not exist",
                                                     "no model", "unknown", "failed to load", "invalid model")):
            raise AgentError('The server does not have the model "%s" ready. Pick a model in '
                             "Settings. The server said: %s" % (self.model, detail))
        if status == 404:
            raise AgentError("%s is not the address of an OpenAI-compatible server (HTTP 404). "
                             "The address usually ends in /v1." % self.url)
        raise AgentError("The model server reported an error (HTTP %d): %s" % (status, detail or "no text"))

    def unreachable(self, exc, timeout):
        if isinstance(exc, TimeoutError):
            return ("The model server did not answer within %d seconds. "
                    "The model may be too large for this computer." % timeout)
        return ("The model server at %s does not answer (%s). Start the server "
                "(in LM Studio: Developer, Start server) and check the address in "
                "Settings." % (self.url, exc.__class__.__name__))


def _block_id(text, used):
    """A tool call id in the letters the Messages API takes, and one that `used` does
    not have yet: that API refuses a conversation in which an id comes twice. Other
    servers make their own ids; some repeat them and some send none."""
    base = re.sub(r"[^A-Za-z0-9_-]", "_", str(text or "")) or "call"
    ident, n = base, 1
    while ident in used:
        n += 1
        ident = "%s_%d" % (base, n)
    used.add(ident)
    return ident


def _tool_name(text):
    """A tool name in the letters and the length the Messages API takes. A small local
    model can ask for "default_api.run_python": one such name must not block the conversation."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(text or ""))[:64] or "tool"


def anthropic_messages(view, picture):
    """The neutral conversation as the "messages" of the Messages API.

    That API wants user and assistant turns in turn, every tool call answered
    by a tool_result in the very next user turn, and no empty text.
    """
    out, used = [], set()       # used: every tool call id that was sent so far
    waiting = []                # (stored id, id that is sent) of the tool calls that have no result yet

    def add(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"] = out[-1]["content"] + blocks
        else:
            out.append({"role": role, "content": blocks})

    def answer_all():
        if waiting:             # a stored conversation that was cut in the middle of a step
            add("user", [{"type": "tool_result", "tool_use_id": sent, "content": "(no result)"}
                         for _, sent in waiting])
            del waiting[:]

    for m in view:
        role, text = m.get("role"), m.get("content")
        text = text if isinstance(text, str) else ""
        if role == "tool":
            stored = str(m.get("tool_call_id") or "")
            pair = next((p for p in waiting if p[0] == stored), None)
            if pair:
                waiting.remove(pair)    # the first open call with this id: results come in the order of the calls
                block = {"type": "tool_result", "tool_use_id": pair[1], "content": text or "(no output)"}
                if text.startswith("ERROR"):
                    block["is_error"] = True
            else:
                block = {"type": "text", "text": "Tool result:\n" + (text or "(no output)")}
            add("user", [block])
        elif role == "assistant":
            answer_all()
            blocks = m.get("_native")
            if blocks:                  # the service's own blocks go back unchanged, with its ids
                pairs = [(str(b.get("id") or ""), b.get("id")) for b in blocks if b.get("type") == "tool_use"]
                used.update(sent for _, sent in pairs)
            else:
                blocks, pairs = ([{"type": "text", "text": text}] if text.strip() else []), []
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = None
                    stored = str(call.get("id") or "")
                    pairs.append((stored, _block_id(stored, used)))
                    blocks.append({"type": "tool_use", "id": pairs[-1][1], "name": _tool_name(fn.get("name")),
                                   "input": args if isinstance(args, dict) else {}})
            if blocks:
                add("assistant", blocks)
                waiting[:] = pairs
        else:
            answer_all()
            blocks, gone = [], []
            for path in m.get("images") or []:
                data = picture(path)
                if data:
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": data[0],
                                                               "data": data[1]}})
                else:
                    gone.append(path)
            if gone:
                text = _add_line(text, PICTURE_GONE % ", ".join(gone))
            if text.strip() or not blocks:
                blocks.append({"type": "text", "text": text if text.strip() else "(no text)"})
            add("user", blocks)
    answer_all()
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(The conversation goes on.)"}]})
    if out and out[-1]["role"] != "user":
        out.append({"role": "user", "content": [{"type": "text", "text": "Go on."}]})
    return out


class AnthropicWire:
    """The Anthropic Messages API: POST <address>/v1/messages."""

    kind = "anthropic"
    version = "2026-10-21"          # the anthropic-version header; this version takes no temperature
    # Claude models have a large context, but every step sends the conversation
    # again and the user pays for it: about 85k tokens are kept.
    max_tokens, context_chars, max_steps = 16000, 300000, 40
    image_chars = 6000              # a picture of 1568 px costs up to about 3000 tokens
    too_long = "The conversation is too long for this model. Start a new chat."
    where = "Anthropic"

    def __init__(self, url, model, key):
        self.url, self.model, self.key = (url or "").strip().rstrip("/"), model, key

    def request(self, system, view, tools, max_tokens, temperature, picture):
        body = {"model": self.model, "max_tokens": max_tokens,
                "messages": anthropic_messages(view, picture),
                # the service keeps the unchanged start of the conversation ready for the
                # next step: later steps of one request cost a fraction
                "cache_control": {"type": "ephemeral"}}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [{"name": fn.get("name") or "tool", "description": fn.get("description") or "",
                              "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}}
                             for fn in (t.get("function") or {} for t in tools)]
        headers = {"Content-Type": "application/json", "x-api-key": self.key,
                   "anthropic-version": self.version}
        return self.url + "/v1/messages", headers, body

    def answer(self, status, headers, raw, pictures):
        data, error, detail = _detail(raw)
        if status == 200 and isinstance(data, dict) and isinstance(data.get("content"), list) and not error:
            return self._message(data)
        kind = error.get("type") if isinstance(error, dict) else ""
        low = detail.lower()
        said = " Anthropic said: %s" % detail if detail else ""
        if status == 401 or kind == "authentication_error":
            raise AgentError("Anthropic did not accept the API key. Check the key in Settings.")
        if status == 402 or kind == "billing_error" or "credit balance" in low:
            raise AgentError("Your Anthropic account has no credit for this request. Add credit in "
                             "the Anthropic Console (Billing), then ask again." + said)
        if status == 429 or kind == "rate_limit_error":
            raise _Busy(_retry_after(headers),
                        "Anthropic asks to wait %d seconds (rate limit of your account). Waiting…",
                        "The rate limit of your Anthropic account is reached. Wait a minute and "
                        "ask again." + said)
        if status in (500, 502, 503, 529) or kind in ("overloaded_error", "api_error"):
            raise _Busy(None, "Anthropic is busy at the moment. Trying again in %d seconds…",
                        "Anthropic is overloaded at the moment (HTTP %d). Try again in a few "
                        "minutes." % status)
        if status == 413 or kind == "request_too_large" or "prompt is too long" in low \
                or ("context" in low and ("window" in low or "length" in low)):
            raise _Overflow()
        if status == 400 and "thinking" in low and ("signature" in low or "bound to" in low):
            raise _BadThinking()
        if status == 404 or kind == "not_found_error":
            raise AgentError('Anthropic does not have the model "%s", or your key may not use it. '
                             "Open Settings, press Find models and pick one." % self.model + said)
        if status == 403 or kind == "permission_error":
            raise AgentError("Your API key is not allowed to do this (HTTP 403)." + said)
        raise AgentError("Anthropic reported an error (HTTP %d): %s" % (status, detail or "no text"))

    def _message(self, data):
        blocks = [b for b in data["content"] if isinstance(b, dict)]
        reason = data.get("stop_reason")
        if reason == "refusal":
            raise AgentError("Anthropic's safety check declined this request. Say it in other words "
                             "and ask again.")
        text = "\n\n".join(b["text"].strip() for b in blocks
                           if b.get("type") == "text" and isinstance(b.get("text"), str) and b["text"].strip())
        msg = {"role": "assistant", "content": text}
        if reason in ("max_tokens", "model_context_window_exceeded"):
            # a tool call that was cut off has half its code: it is not run
            msg["content"] = _add_line(text, CUT_OFF)
            msg["_cut_off"] = True     # for the loop: the model was stopped in the middle of its work
            return msg
        calls = [{"id": b.get("id"), "type": "function",
                  "function": {"name": b.get("name") or "", "arguments": json.dumps(b.get("input") or {})}}
                 for b in blocks if b.get("type") == "tool_use"]
        if calls:
            msg["tool_calls"] = calls
        if any(b.get("type") in ("thinking", "redacted_thinking") for b in blocks):
            # Claude's thinking comes as signed blocks. They go back unchanged with the
            # next steps of this request, and are never stored or shown.
            msg["_native"] = blocks
        return msg

    def unreachable(self, exc, timeout):
        if isinstance(exc, TimeoutError):
            return "Anthropic did not answer within %d seconds. Ask again." % timeout
        return ("No connection to Anthropic (%s). Check the internet connection of this "
                "computer." % exc.__class__.__name__)


WIRES = {"openai": OpenAIWire, "anthropic": AnthropicWire}

PROXY = (" This computer is set to reach the internet through a proxy. CADprompt does not use a "
         "proxy: it connects directly, and this network may not allow that.")


def proxy_note(address):
    """PROXY when the system settings name a proxy for this https address, else "".

    The engine and Find models connect directly, so that a key goes to its own
    server and to no other host. On a network that lets a computer out only
    through its proxy the request fails, and the user must know why.
    """
    try:
        u = urllib.parse.urlsplit(address)
        if u.scheme == "https" and urllib.request.getproxies().get("https") \
                and not urllib.request.proxy_bypass(u.netloc):
            return PROXY
    except Exception:                  # a broken system setting must not hide the error itself
        pass
    return ""


class Agent:
    """One conversation with the model. `messages` is the stored history (without
    the system prompt); a new Agent with the same list continues the conversation.

    kind is the wire format: "openai" or "anthropic".
    run_tool(name, args) -> str runs a tool; it may raise Stopped.
    may_contact(kind, url) -> bool is asked before every request; False sends nothing.
    load_image(path) -> (media type, base64 text) reads a picture of the conversation.
    on_event(kind, data) reports progress:
        "state"       list   a copy of the messages at a consistent point (safe to store)
        "text"        str    text of the model that came with tool calls
        "note"        str    what the loop itself did (automatic check, nudge, waiting)
        "tool_call"   {"name", "args"}
        "tool_result" {"name", "result", "problem"}
        "answer"      str    the final answer
    stop() may be called from another thread. After it, ask() raises Stopped;
    use a new Agent for the next request.
    """

    def __init__(self, url, model, run_tool, key="", system="", tools=None, messages=None,
                 on_event=None, max_steps=None, max_tokens=None, temperature=0.2, timeout=600,
                 context_chars=None, kind="openai", may_contact=None, load_image=None,
                 connect_timeout=None):
        self.key = (key or "").strip()
        self.model = (model or "").strip()
        self.wire = WIRES[kind](url, self.model, self.key)
        self.url = self.wire.url
        self.run_tool = run_tool
        self.system = system
        self.tools = tools or []
        self.messages = list(messages or [])
        self.on_event = on_event or (lambda kind, data: None)
        self.max_steps = max_steps or self.wire.max_steps
        self.max_tokens = max_tokens or self.wire.max_tokens
        self.temperature = temperature
        self.timeout = timeout                    # seconds for each step of an answer
        self.connect_timeout = connect_timeout    # seconds to reach the server; None: CONNECT_TIMEOUT
        self.context_chars = context_chars or self.wire.context_chars
        self.may_contact = may_contact
        self.load_image = load_image
        self._stop = threading.Event()
        self._conn = None
        self._calls = 0
        self._pictures = {}       # path -> what load_image gave, so every step sends the same bytes
        self._no_pictures = False  # this model cannot read pictures: none is sent
        self.incomplete = False   # after ask(): the step limit, a cut answer, or a problem not repaired

    # ---- stop ---------------------------------------------------------------
    def stop(self):
        """Stop as soon as possible. Also ends a request that waits for the server,
        so a local server does not go on computing an answer nobody reads."""
        self._stop.set()
        conn = self._conn
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stopped(self):
        return self._stop.is_set()

    def _check_stop(self):
        if self._stop.is_set():
            raise Stopped()

    # ---- the loop -----------------------------------------------------------
    def ask(self, text, shown=None, images=None):
        """Run one request of the user to its end. Returns the final answer text.
        `images`: paths of at most MAX_IMAGES pictures that go with the request."""
        if not self.url:
            raise AgentError("No server address is set. Open Settings and enter the address "
                             "of the model server.")
        if not self.model:
            raise AgentError("No model is set. Open Settings and pick a model.")
        images = list(images or [])[-MAX_IMAGES:]
        for path in images:
            if self._picture(path) is None:
                raise AgentError(BAD_PICTURE % path)
        first = len(self.messages)
        self.messages.append(user_message(text, shown, images))
        self._state()
        try:
            return self._loop()
        except AgentError:
            if len(self.messages) == first + 1:
                # nothing happened: forget the request, so a second try does not double it
                del self.messages[first:]
                self._state()
            raise

    def _state(self):
        self.on_event("state", public(self.messages))

    def _picture(self, path):
        """(media type, base64 text) of a picture, or None when it cannot be read."""
        if path not in self._pictures:
            try:
                self._pictures[path] = self.load_image(path) if self.load_image else None
            except Stopped:
                raise
            except Exception:
                self._pictures[path] = None
        return self._pictures[path]

    def _loop(self):
        problem, pushes, nudged = "", 0, False
        failed = None       # the last call when it failed: a small model likes to send it again
        error = ""          # what that call reported, kept for as long as the model repeats it
        for _ in range(self.max_steps):
            self._check_stop()
            msg = self._chat()
            self._check_stop()
            calls = self._tool_calls(msg)
            content = _visible(msg.get("content"))     # reasoning_content is never kept
            native = {"_native": msg["_native"]} if msg.get("_native") else {}
            if not calls and not content:
                if not nudged:
                    nudged = True
                    self.messages.append({"role": "user", "auto": True, "content": NUDGE})
                    self.on_event("note", "The model gave an empty answer. Asking again.")
                    continue
                return self._final(NO_ANSWER, problem)
            if not calls and problem and pushes < 2:
                pushes += 1
                self.messages.append(dict({"role": "assistant", "content": content}, **native))
                self.messages.append({"role": "user", "auto": True, "content": CHECK % problem})
                self._state()
                self.on_event("text", content)
                self.on_event("note", "Automatic check: there is still a problem. The model continues.")
                continue
            if not calls:
                if msg.get("_cut_off"):
                    self.incomplete = True      # half an answer: the work can be half done too
                return self._final(content, problem)
            self.messages.append(dict({"role": "assistant", "content": content, "tool_calls": calls}, **native))
            if content:
                self.on_event("text", content)
            for call in calls:
                self._check_stop()
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                again = (name, arguments) == failed
                result = self._run(name, arguments, SAME_CALL % error[-400:] if again else None)
                self._check_stop()
                bad = find_problem(name, result)
                failed = (name, arguments) if bad else None
                if not again:
                    error = bad         # a refusal quotes the first error, not the refusal before it
                if name in REPAIR:
                    # "no solid to draw" or "no such file" is for the user: the model cannot repair it
                    problem = bad
                self.on_event("tool_result", {"name": name, "result": result, "problem": bool(bad)})
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            self._state()
        self.incomplete = True
        return self._final(STEP_LIMIT % self.max_steps, problem)

    def _final(self, text, problem):
        if problem:
            self.incomplete = True
            text += "\n\n" + STILL_WRONG
        self.messages.append({"role": "assistant", "content": text})
        self._state()
        self.on_event("answer", text)
        return text

    def _tool_calls(self, msg):
        """The tool calls of an answer, with only the fields that go back to the server."""
        out = []
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, dict):
                args = json.dumps(args)
            self._calls += 1
            out.append({"id": call.get("id") or "call_%d" % self._calls, "type": "function",
                        "function": {"name": fn.get("name") or "", "arguments": args or "{}"}})
        return out

    def _run(self, name, arguments, instead=None):
        """Run one tool call. `instead` is the result to give without running it."""
        try:
            args = json.loads(arguments)
            if not isinstance(args, dict):
                raise ValueError("not an object")
        except ValueError as exc:
            return "ERROR: the tool arguments are not valid JSON: %s" % exc
        self.on_event("tool_call", {"name": name, "args": args})
        if instead:
            return instead
        result = self.run_tool(name, args)
        return result if isinstance(result, str) else str(result)

    # ---- HTTP ---------------------------------------------------------------
    def _chat(self):
        """One answer of the model to the conversation as it is now."""
        wire = self.wire
        budget = self.context_chars - len(self.system) - len(json.dumps(self.tools))
        halved, bare, waits = False, False, 0
        while True:
            view, dropped = build_view(self.messages, max(budget, 2000), wire.image_chars,
                                       images=not self._no_pictures)
            system = self.system
            if dropped:
                system += "\n\nThe start of this conversation was left out to save space."
            try:
                return self._post(system, view)
            except _Refused as exc:
                wire.refused.add(str(exc))                # send again without it, and remember
            except _Overflow:
                if halved:
                    raise AgentError(wire.too_long)
                halved = True
                budget = min(budget, _size(view, wire.image_chars)) // 2    # send half as much, once
            except _Busy as exc:
                seconds = exc.seconds if exc.seconds is not None else BACKOFF * 2 ** waits    # 2, 4, 8
                waits += 1
                if waits > RETRIES or seconds > LONGEST_WAIT:
                    raise AgentError(exc.text)
                self.on_event("note", exc.note % max(1, round(seconds)))
                if self._stop.wait(seconds):              # Stop also ends the wait
                    raise Stopped()
            except _BadThinking:
                if bare:
                    raise AgentError("Anthropic did not accept the conversation. Start a new chat.")
                bare = True
                for m in self.messages:                   # go on without the thinking of earlier steps
                    m.pop("_native", None)
            except _NoPictures:
                asked = [m for m in self.messages if m.get("role") == "user" and not m.get("auto")]
                if asked and asked[-1].get("images"):
                    raise NoVision(NO_VISION)             # the user decides: they asked about a picture
                self._no_pictures = True
                self.on_event("note", NO_VISION_NOTE)

    def _post(self, system, view):
        wire = self.wire
        if self.may_contact is not None and not self.may_contact(wire.kind, wire.url):
            raise AgentError(NOT_ALLOWED % wire.where)
        address, headers, body = wire.request(system, view, self.tools, self.max_tokens,
                                              self.temperature, self._picture)
        pictures = any(m.get("images") for m in view)
        u = urllib.parse.urlsplit(address)
        try:
            conn = connect(address, self.timeout, self.connect_timeout)
            self._conn = conn
            try:
                self._check_stop()     # stop() either saw the socket or we see its flag here
                conn.request("POST", u.path + ("?" + u.query if u.query else ""),
                             json.dumps(body).encode("utf-8"), headers)
                resp = conn.getresponse()
                status, raw = resp.status, resp.read()
                got = {k.lower(): v for k, v in resp.getheaders()}
            finally:
                self._conn = None
                conn.close()
        except Stopped:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if self._stop.is_set():
                raise Stopped()
            raise AgentError(wire.unreachable(exc, self.timeout) + proxy_note(address))
        return wire.answer(status, got, raw, pictures)
