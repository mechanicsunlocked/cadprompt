"""The chat loop for a local model: OpenAI chat completions with tool calls.

Pure Python (no Qt, no FreeCAD), so it also runs in plain python3 tests.
The loop sends the conversation to an OpenAI-compatible server (LM Studio,
Ollama, llama.cpp, or a provider with a key), runs the tools the model asks
for through `run_tool`, and goes on until the model answers without a tool
call. It is written for small models: they forget to check their work, give
empty answers and have a short context, so the loop checks, nudges and
shortens for them.
"""

import http.client
import json
import re
import socket
import threading
import urllib.parse

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

SENT_KEYS = ("role", "content", "tool_calls", "tool_call_id")   # all a server may see of a message


class Stopped(Exception):
    """The user stopped the run."""


class AgentError(Exception):
    """The run cannot go on. The text is written for the user."""


class _Overflow(Exception):
    """The server says the conversation does not fit the model's context."""


def user_message(text, shown=None):
    """A request of the user. `shown` is what they typed when `text` is an expanded command."""
    msg = {"role": "user", "content": text}
    if shown and shown != text:
        msg["shown"] = shown
    return msg


def find_problem(name, result):
    """The part of a tool result that the model must not ignore, or ''."""
    bad = result.startswith("ERROR")
    if not bad and name in ("run_python", "get_document_state"):
        bad = any(line.startswith(("ERROR", "PROBLEM OBJECTS")) for line in result.splitlines())
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


def _size(messages):
    return len(json.dumps([{k: m[k] for k in SENT_KEYS if k in m} for m in messages]))


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


def build_view(messages, budget):
    """What is sent of the stored conversation: (messages, something_left_out).

    The stored list stays complete. When it is larger than `budget` characters:
    old tool results and old code are cut first, then whole old requests are
    left out, then the current request is cut harder.
    """
    msgs, dropped = list(messages), False
    if _size(msgs) > budget:
        msgs = _shorten(msgs, 2, 300)
    if _size(msgs) > budget:
        starts = [i for i, m in enumerate(msgs) if m.get("role") == "user" and not m.get("auto")]
        for start in starts[1:]:
            dropped = True
            if _size(msgs[start:]) <= budget or start == starts[-1]:
                msgs = msgs[start:]
                break
    if _size(msgs) > budget:
        msgs = _shorten(msgs, 1, 150)
        last = max((i for i, m in enumerate(msgs) if m.get("role") == "tool"), default=None)
        if last is not None:
            msgs[last] = dict(msgs[last], content=_cut(msgs[last].get("content") or "", 2000))
    out = []
    for m in msgs:
        m = {k: m[k] for k in SENT_KEYS if k in m}
        if m["role"] == "user" and out and out[-1]["role"] == "user":
            # some chat templates refuse two user messages in a row
            out[-1] = dict(out[-1], content=out[-1]["content"] + "\n\n" + m["content"])
        else:
            out.append(m)
    return out, dropped


class Agent:
    """One conversation with the model. `messages` is the stored history (without
    the system prompt); a new Agent with the same list continues the conversation.

    run_tool(name, args) -> str runs a tool; it may raise Stopped.
    on_event(kind, data) reports progress:
        "state"       list   a copy of the messages at a consistent point (safe to store)
        "text"        str    text of the model that came with tool calls
        "note"        str    what the loop itself did (automatic check, nudge)
        "tool_call"   {"name", "args"}
        "tool_result" {"name", "result", "problem"}
        "answer"      str    the final answer
    stop() may be called from another thread. After it, ask() raises Stopped;
    use a new Agent for the next request.
    """

    def __init__(self, url, model, run_tool, key="", system="", tools=None, messages=None,
                 on_event=None, max_steps=20, max_tokens=6000, temperature=0.2, timeout=600,
                 context_chars=12000):
        url = (url or "").strip().rstrip("/")
        if url and "://" not in url:
            url = "http://" + url
        self.url = url
        self.model = (model or "").strip()
        self.key = (key or "").strip()
        self.run_tool = run_tool
        self.system = system
        self.tools = tools or []
        self.messages = list(messages or [])
        self.on_event = on_event or (lambda kind, data: None)
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.context_chars = context_chars   # about 3 characters per token: 12000 leaves room in 8k
        self._stop = threading.Event()
        self._conn = None
        self._calls = 0

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
    def ask(self, text, shown=None):
        """Run one request of the user to its end. Returns the final answer text."""
        if not self.url:
            raise AgentError("No server address is set. Open Settings and enter the address "
                             "of the model server.")
        if not self.model:
            raise AgentError("No model is set. Open Settings and pick a model.")
        first = len(self.messages)
        self.messages.append(user_message(text, shown))
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
        self.on_event("state", list(self.messages))

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
            if not calls and not content:
                if not nudged:
                    nudged = True
                    self.messages.append({"role": "user", "auto": True, "content": NUDGE})
                    self.on_event("note", "The model gave an empty answer. Asking again.")
                    continue
                return self._final(NO_ANSWER, problem)
            if not calls and problem and pushes < 2:
                pushes += 1
                self.messages.append({"role": "assistant", "content": content})
                self.messages.append({"role": "user", "auto": True, "content": CHECK % problem})
                self._state()
                self.on_event("text", content)
                self.on_event("note", "Automatic check: there is still a problem. The model continues.")
                continue
            if not calls:
                return self._final(content, problem)
            keep = {"role": "assistant", "content": content, "tool_calls": calls}
            self.messages.append(keep)
            if content:
                self.on_event("text", content)
            for call in calls:
                self._check_stop()
                name, arguments = call["function"]["name"], call["function"]["arguments"]
                again = (name, arguments) == failed
                result = self._run(name, arguments, SAME_CALL % error[-400:] if again else None)
                self._check_stop()
                problem = find_problem(name, result)
                failed = (name, arguments) if problem else None
                if not again:
                    error = problem     # a refusal quotes the first error, not the refusal before it
                self.on_event("tool_result", {"name": name, "result": result, "problem": bool(problem)})
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            self._state()
        return self._final(STEP_LIMIT % self.max_steps, problem)

    def _final(self, text, problem):
        if problem:
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
        budget = self.context_chars - len(self.system) - len(json.dumps(self.tools))
        for attempt in (1, 2):
            view, dropped = build_view(self.messages, max(budget, 2000))
            system = self.system
            if dropped:
                system += "\n\nThe start of this conversation was left out to save space."
            body = {"model": self.model,
                    "messages": [{"role": "system", "content": system}] + view,
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature}
            if self.tools:
                body["tools"] = self.tools
            try:
                return self._post(body)
            except _Overflow:
                if attempt == 2:
                    raise AgentError("The conversation is too long for this model. Start a new chat, "
                                     "or load the model with a larger context length.")
                budget = min(budget, _size(view)) // 2    # send half as much, once

    def _post(self, body):
        u = urllib.parse.urlsplit(self.url + "/chat/completions")
        https = u.scheme == "https"
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        try:
            cls = http.client.HTTPSConnection if https else http.client.HTTPConnection
            conn = cls(u.hostname or "", u.port, timeout=self.timeout)
            self._conn = conn
            try:
                conn.connect()
                self._check_stop()     # stop() either saw the socket or we see its flag here
                conn.request("POST", u.path + ("?" + u.query if u.query else ""),
                             json.dumps(body).encode("utf-8"), headers)
                resp = conn.getresponse()
                status, raw = resp.status, resp.read()
            finally:
                self._conn = None
                conn.close()
        except Stopped:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if self._stop.is_set():
                raise Stopped()
            if isinstance(exc, TimeoutError):
                raise AgentError("The model server did not answer within %d seconds. "
                                 "The model may be too large for this computer." % self.timeout)
            raise AgentError("The model server at %s does not answer (%s). Start the server "
                             "(in LM Studio: Developer, Start server) and check the address in "
                             "Settings." % (self.url, exc.__class__.__name__))
        return self._message(status, raw)

    def _message(self, status, raw):
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        error = data.get("error") if isinstance(data, dict) else None
        if status == 200 and not error:
            try:
                msg = data["choices"][0]["message"]
                if isinstance(msg, dict):
                    return msg
            except (KeyError, IndexError, TypeError):
                pass
            raise AgentError("The answer of the server at %s was not understood. Is it an "
                             "OpenAI-compatible server? The address usually ends in /v1." % self.url)
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("code") or error)
        else:
            detail = str(error or text)
        detail = " ".join(detail.split())[:300]
        low = detail.lower()
        if status in (401, 403):
            raise AgentError("The server did not accept the API key (HTTP %d). Check the key in "
                             "Settings." % status)
        if ("context" in low and any(w in low for w in ("length", "window", "exceed", "overflow", "size"))) \
                or "too many tokens" in low or "too long" in low:
            raise _Overflow()
        if "model" in low and any(w in low for w in ("not found", "not loaded", "does not exist",
                                                     "no model", "unknown", "failed to load", "invalid model")):
            raise AgentError('The server does not have the model "%s" ready. Pick a model in '
                             "Settings. The server said: %s" % (self.model, detail))
        if status == 404:
            raise AgentError("%s is not the address of an OpenAI-compatible server (HTTP 404). "
                             "The address usually ends in /v1." % self.url)
        raise AgentError("The model server reported an error (HTTP %d): %s" % (status, detail or "no text"))
