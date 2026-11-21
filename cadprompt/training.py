# SPDX-License-Identifier: MIT
"""Training data: good requests of the built-in engine, kept as one file in the project.

Off in a new installation (parameter TrainingData, one check box in Settings).
While it is on, each request of the built-in engine that ended without a
problem becomes one line of JSON in <project>/training/requests.jsonl, to
fine-tune a small local model later. Nothing is sent anywhere: nothing in
here opens a connection. Requests that Claude Code ran are not written.

One line is one request: the system prompt, the names of the tools, and the
messages in the neutral shape of local_agent. The earlier messages of the
conversation, as the model got them with the first step, come first and have
"context": true.

Never in the file: a saved key or the token of the bridge, the pixels of an
image, the text that read_project_file returned (refs/ can hold pages of
school books), a key of a message that this module does not know (so none
that starts with "_"), and the paths of the project and of the home folder:
PROJECT and ~ stand for them.
"""

import json
import os
import re
import stat
import time

from . import local_agent

PARAM = "TrainingData"
FOLDER, FILE = "training", "requests.jsonl"
VERSION = 1              # of the line format; raise it when a field changes its meaning

LABEL = "Save good requests as training data in the project"
HINT = ("On: when a request to a local model or with an API key ends without a problem, it is added to "
        "the file training/requests.jsonl in the project folder: your messages, the AI's code and "
        "what the tools reported. Nothing is sent anywhere. Requests that Claude Code ran are not saved.")
SAVED = ("This request was saved as training data in `training/requests.jsonl` in the project folder, "
         "and so are the next good ones. Nothing is sent. Turn it off in Settings.")
NOT_WRITTEN = ("The training data could not be written to `training/requests.jsonl` in the project "
               "folder: %s. Your request is not affected.")
A_LINK = "the file or its folder is a link, or not a normal file"

# what stands in the file for the things that are left out
NO_PIXELS = "not exported: only the paths of the images in the project"
FILE_TEXT = "(read_project_file: the text of %s is not exported, %d characters)"
QUOTED = "(text of a project file, not exported)"
UNKNOWN = "(tool result not exported: its call is not known, %d characters)"
IMAGE_DATA = "(image data, not exported)"
ENCODED = "(encoded data, not exported: %d characters)"

KEPT = ("role", "content", "tool_calls", "tool_call_id", "images", "shown", "auto")   # of a message
# The loop checks the results of run_python and get_document_state itself (local_agent.REPAIR).
# For these two it does not: a request is good only when the last result of each is no error.
OWN_CHECK = ("make_drawing", "read_project_file")
QUOTE = 40               # a line of a project file of this length is also found where it is quoted

_SEP = r"[\\/]+"         # both slash directions, and the doubled backslash of JSON text and of repr()
_DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{16,}")
_ENCODED = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")      # base64 with no "data:" before it
# Another spelling of a home folder: the short name that Windows gives a long one, another user.
_USER_FOLDER = re.compile(r"(?<![\w.~-])(?:[A-Za-z]:)?" + _SEP + r"(?:home|users)" + _SEP
                          + r"[^\\/\s\"'`<>|:*?,;()\[\]{}]+", re.I)

_told = set()            # the projects whose file the chat named in this session
_failed = []             # not empty: the chat said once in this session that a write failed


def enabled():
    """True when the user turned the training data on. Off in a new profile."""
    from . import paths
    return paths.params().GetBool(PARAM, False)


def set_enabled(on):
    from . import paths
    paths.params().SetBool(PARAM, bool(on))


# ---- which requests ------------------------------------------------------------
def _paired(messages):
    """{index of a tool message: (tool name, arguments or None)}.

    A result answers a call of the assistant message before it. The ids are
    those of the model server: some repeat them, so the first open call with
    the id is the one, as in local_agent.anthropic_messages.
    """
    out, waiting = {}, []
    for i, m in enumerate(messages):
        role = m.get("role") if isinstance(m, dict) else None
        if role == "assistant":
            waiting = []
            for call in m.get("tool_calls") or []:
                fn = (call.get("function") if isinstance(call, dict) else None) or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = None
                waiting.append((str(call.get("id") or ""), str(fn.get("name") or ""),
                                args if isinstance(args, dict) else None))
        elif role == "tool":
            ident = str(m.get("tool_call_id") or "")
            hit = next((w for w in waiting if w[0] == ident), None)
            if hit:
                waiting.remove(hit)
                out[i] = hit[1:]
    return out


def ended_well(res, saved, request):
    """True for a request that ended without a problem. Only such a request is written.

    res      what LocalRun reports when the request ends ({"is_error", "result", "incomplete"})
    saved    the engine saved the model after it
    request  its messages, from the user's message to the final answer

    Not good: an error of the engine, a request that was stopped (the engine
    does not come here for it), the step limit, a cut answer or a problem that
    the model did not repair (all three are Agent.incomplete, and the model is
    not saved then), no answer of the model at all, a step that the user did
    not approve, and a request that changed nothing (no model was saved). For
    make_drawing and read_project_file the engine has no automatic check: here
    their last result must be without an error (OWN_CHECK). A tool error that
    the model repaired afterwards is no problem: the error and the repair are
    both written.
    """
    from . import approval
    if res.get("is_error") or res.get("stopped") or res.get("incomplete") or not saved:
        return False
    if len(request) < 2 or request[0].get("role") != "user" or request[0].get("auto"):
        return False
    last = request[-1]
    answer = last.get("content")
    if last.get("role") != "assistant" or last.get("tool_calls") or not isinstance(answer, str) \
            or not answer.strip() or answer == local_agent.NO_ANSWER:
        return False
    if local_agent.STILL_WRONG in answer or local_agent.CUT_OFF in answer:
        return False                 # a second lock: res["incomplete"] says the same
    pairs, left = _paired(request), {}
    for i, m in enumerate(request):
        if m.get("role") != "tool":
            continue
        result = m.get("content")
        if i not in pairs or not isinstance(result, str) or result == approval.DECLINED:
            return False
        name = pairs[i][0]
        if name in OWN_CHECK:
            left[name] = bool(local_agent.find_problem(name, result))
    return not any(left.values())


def begin(source, mode, agent, messages):
    """What is known of a request when it starts: its fields, and the earlier messages as
    the model gets them with the first step (old results cut, old requests left out).

    `messages` is the conversation with the new message of the user at its end.
    """
    view, dropped = local_agent.build_view(messages, max(agent.room(), 2000), agent.wire.image_chars)
    return {"source": source, "model": agent.model, "mode": "teach" if mode == "teach" else "build",
            "system": agent.system + (local_agent.LEFT_OUT if dropped else ""),
            "tools": [str((t.get("function") or {}).get("name") or "") for t in agent.tools],
            "context": view[:-1], "keys": [agent.key]}


# ---- what goes into the line -----------------------------------------------------
def _spellings(folder):
    """The names of one folder: as given, absolute, and without links (macOS: /var is /private/var)."""
    if not folder:
        return set()
    # not the top of a disk: "/" or "C:\\" would be found in every path
    return {path for path in (folder, os.path.abspath(folder), os.path.realpath(folder))
            if os.path.dirname(path) != path}


def _homes():
    """The home folder of the user, in every spelling that this computer gives."""
    out = set()
    for path in (os.path.expanduser("~"), os.environ.get("HOME"), os.environ.get("USERPROFILE")):
        if path and os.path.isabs(path):
            out |= _spellings(path)
    return out


def _path_rule(path):
    """A pattern for one folder in a text: upper and lower case, both slash directions."""
    parts = [re.escape(p) for p in re.split(_SEP, path) if p]
    start = _SEP if path[:1] in "\\/" else ""
    # the next letter is not a part of the name: /home/max is not found in /home/maximilian
    return re.compile(start + _SEP.join(parts) + r"(?![\w-])", re.I)


def _secrets(more=()):
    """(text, what is written for it) of the keys: the same list that hides them in a tool result."""
    from . import local_tools
    return local_tools._secrets() + [(key, local_tools.A_KEY) for key in more
                                     if isinstance(key, str) and len(key) >= local_tools.KEY_LENGTH]


def _quoted(messages):
    """The long lines of the project files that read_project_file gave to the model."""
    lines = set()
    for i, (name, _) in _paired(messages).items():
        text = messages[i].get("content")
        if name == "read_project_file" and isinstance(text, str) and not text.startswith("ERROR"):
            lines.update(line.strip() for line in text.split("\n")[1:] if len(line.strip()) >= QUOTE)
    return sorted(lines, key=len, reverse=True)


class _Cleaner:
    """Takes out of the messages what must not be in the file."""

    def __init__(self, project, quoted=(), keys=()):
        self.secrets = _secrets(keys)
        self.quoted = list(quoted)
        longest = lambda paths: sorted(paths, key=len, reverse=True)    # /var/home/x before /home/x
        self.paths = [(_path_rule(p), "PROJECT") for p in longest(_spellings(project))]
        self.paths += [(_path_rule(p), "~") for p in longest(_homes())]
        self.paths.append((_USER_FOLDER, "~"))

    def text(self, text):
        for secret, word in self.secrets:
            text = text.replace(secret, word)
        for line in self.quoted:
            text = text.replace(line, QUOTED)
        text = _DATA_URI.sub(IMAGE_DATA, text)
        for rule, word in self.paths:
            text = rule.sub(lambda _m, word=word: word, text)
        return _ENCODED.sub(lambda m: ENCODED % len(m.group()), text)

    def value(self, value):
        """The same value with every text in it cleaned."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {(self.text(k) if isinstance(k, str) else k): self.value(v) for k, v in value.items()}
        return value

    def _call(self, call):
        fn = (call.get("function") if isinstance(call, dict) else None) or {}
        raw = fn.get("arguments")
        try:
            args = json.loads(raw)
        except (TypeError, ValueError):
            args = None
        if isinstance(args, (dict, list)):
            # as values, not as JSON text: there a path has doubled backslashes and ü for ü
            clean = self.value(args)
            raw = raw if clean == args else json.dumps(clean, ensure_ascii=False)
        else:
            raw = self.text(str(raw or ""))
        return {"id": self.text(str(call.get("id") or "")), "type": "function",
                "function": {"name": self.text(str(fn.get("name") or "")), "arguments": raw}}

    def _result(self, content, call):
        text = content if isinstance(content, str) else json.dumps(content)
        if call is None:
            return UNKNOWN % len(text)       # it can be the text of a file: we do not know the tool
        name, args = call
        if name == "read_project_file" and not text.startswith("ERROR"):
            path = args.get("path") if args else ""
            return FILE_TEXT % (self.text(str(path or "refs/INDEX.md")), len(text))
        return self.text(text)

    def message(self, m, call, context):
        """One message for the file. `call`: (name, arguments) of the call that a tool result answers."""
        out = {}
        for key in KEPT:
            if key not in m:
                continue
            if key == "content" and m.get("role") == "tool":
                out[key] = self._result(m[key], call)
            elif key == "tool_calls":
                out[key] = [self._call(c) for c in m[key] or [] if isinstance(c, dict)]
            else:
                out[key] = self.value(m[key])
        if out.get("images"):
            out["image_data"] = NO_PIXELS
        if context:
            out["context"] = True
        return out


def record(project, info, request, stored=()):
    """One request as the dict of its line.

    info     what begin() gave
    request  the messages of the request, in full
    stored   the whole conversation, with the request at its end: to find the text
             of a project file that another message quotes (a long line of it, as
             it is; a short or reworded quote is not found)
    """
    cleaner = _Cleaner(project, _quoted([m for m in stored or request if isinstance(m, dict)]),
                       info.get("keys") or ())

    def clean(messages, context):
        messages = [m for m in messages if isinstance(m, dict)]
        pairs = _paired(messages)
        return [cleaner.message(m, pairs.get(i), context) for i, m in enumerate(messages)]

    return {"version": VERSION, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": info["source"], "model": cleaner.text(info["model"]), "mode": info["mode"],
            "system": cleaner.text(info["system"]), "tools": [cleaner.text(t) for t in info["tools"]],
            "messages": clean(info.get("context") or [], True) + clean(request, False)}


def line(rec):
    """The bytes of one line: UTF-8, one LF at the end and no other line end inside."""
    text = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
    for mark in ("\x85", " ", " "):          # line ends for some readers; JSON leaves them as they are
        text = text.replace(mark, "\\u%04x" % ord(mark))
    # backslashreplace: half a character from a broken file name becomes its JSON escape
    return text.encode("utf-8", "backslashreplace") + b"\n"


# ---- the file ----------------------------------------------------------------------
def path_of(project):
    return os.path.join(project, FOLDER, FILE)


def append(project, data):
    """Add the bytes of one line to <project>/training/requests.jsonl. Raises OSError.

    Only inside the project: not when training/ is a link (on Windows a
    junction) or the file is a link of any kind, the rule of projects._put.
    The folder and the file are made for the current user only, where the
    system has file modes.
    """
    from . import projects
    root = os.path.realpath(project)
    folder, path = os.path.join(project, FOLDER), path_of(project)
    if not os.path.isdir(root):
        raise OSError("there is no project folder")
    if os.path.islink(folder) or not projects._in_project(project, FOLDER + "/" + FILE):
        raise OSError(A_LINK)
    if not os.path.lexists(folder):
        os.mkdir(folder, 0o700)
    if os.path.lexists(path):
        st = os.lstat(path)
        real = os.path.normcase(os.path.realpath(path))
        # more than one name (a hard link): the line would also go into the file with the other name
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1 \
                or real != os.path.normcase(os.path.join(root, FOLDER, FILE)):
            raise OSError(A_LINK)
    # O_NOFOLLOW: also not through a link that was put there after the look above
    flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink > 1:
            raise OSError(A_LINK)
        if os.name == "posix" and st.st_mode & 0o077:
            os.fchmod(fd, 0o600)             # made by hand or brought back by a sync tool
        if st.st_size:
            os.lseek(fd, -1, os.SEEK_END)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data          # a last line that has no end: ours must not join it
        while data:                          # O_APPEND: every write goes to the end
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def _why(exc):
    if isinstance(exc, OSError):
        return exc.strerror or str(exc) or exc.__class__.__name__
    return exc.__class__.__name__


def keep(project, info, stored, first, res, saved):
    """Write the request that just ended, when it ended well. Returns one line for the chat, or "".

    stored   the conversation as it is stored; the request begins at `first`
    Never raises: a file that cannot be written must not be in the way of
    the request. The chat hears of it once in a session, and once for each
    project that its requests are saved now.
    """
    try:
        request = [m for m in stored[first:] if isinstance(m, dict)]
        if not ended_well(res, saved, request):
            return ""
        append(project, line(record(project, info, request, stored)))
        place = os.path.normcase(os.path.realpath(project))
    except Exception as exc:
        if _failed:
            return ""
        _failed.append(True)
        return NOT_WRITTEN % _why(exc)
    if place in _told:
        return ""
    _told.add(place)
    return SAVED
