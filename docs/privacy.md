# Safety and privacy, the honest version

- **The AI writes Python and FreeCAD runs it, as you.** You see the code first
  and press Run, unless you turned "Run the AI's code without asking" on. A
  guard refuses the plain ways to start programs, use the network or delete
  files, and one run is stopped after 60 seconds. That guard is a fence, not a
  wall. Read the code, and work on models that you can restore.
- **A small local model makes mistakes.** It can build a wrong part and say that
  it is right. The panel finds broken objects and takes such a step back, but it
  cannot see a hole in the wrong place. Check the part.
- **Keys** are files in FreeCAD's user data folder (`cadprompt_api_key`,
  `cadprompt_local_key`, `cadprompt_endpoint_token`), not in FreeCAD's settings.
  On Linux and macOS only you can read them (mode 0600). Windows has no such
  modes: there the files are in your own user folder, which other users of the
  computer cannot open. A key is sent only to the server it was saved for. The
  Anthropic key always goes over https. The key of a model server goes the way
  the address of that server says: with an address that starts with `http://`
  the connection is not encrypted. For a server on this computer that is no
  problem. For a server on another computer (the classroom server), other
  people on the same network can read the key and the conversation, and the
  question before the first request says so. An address that you type without
  `http://` or `https://` gets https when a key is saved, unless the server is
  this computer.
- **The chat shows text only.** A picture that an answer or a stored
  conversation names is not loaded: the text of the AI cannot make FreeCAD open
  a file or a network address. A link opens in your browser, and only a web
  link (http, https).
- **Nothing is sent to an AI service until you send a message or press Find
  models, and never before you said Allow to that service.** With the Claude
  account source, the panel asks the Claude Code program on this computer
  whether you are signed in (`claude auth status`), but only after you allowed
  Claude Code. No telemetry. Your yes is kept in FreeCAD's settings for the
  service (its kind and its host name) and for the kinds of data that this
  version sends. CADprompt asks again when the host changes, when a later
  version sends another kind of data, and when the address of a server that you
  allowed with `https://` changes to `http://`.
- **A key that is in the environment of your computer** (`ANTHROPIC_API_KEY`
  and the like) is never used by CADprompt. For Claude Code, CADprompt takes
  out of the environment every variable that gives it a key, a login or
  another address, or that sends it to another provider than the one you
  allowed (the names that start with `ANTHROPIC_` and `CLAUDE_CODE_USE_`, for
  example `CLAUDE_CODE_USE_BEDROCK`). The variables that only choose a model
  (`ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_..._MODEL`) stay. A provider that is
  set in Claude Code's own settings file is not changed by CADprompt.
- With the API key source, each step of a request is one HTTPS request to
  `api.anthropic.com`: the conversation of the project (up to about 300,000
  characters of it), the images of that conversation and the key. Find models is
  one more request with the key. Nothing else is contacted, except the proxy of
  your computer when its settings name one (next point).
- When the settings of the computer name a proxy, CADprompt uses it for https,
  as a browser does: the proxy learns the name of the service
  (`api.anthropic.com`) and passes encrypted data that it cannot read. The key
  and the conversation stay inside that encryption. A login for the proxy that
  is part of the setting goes to the proxy only. If the proxy does not answer,
  CADprompt connects directly. A model server with an `http://` address is
  never reached through a proxy, and a model server on this computer is never
  reached through a proxy at all.
- With the Claude account source, Claude Code is a program of its own, and it
  must reach FreeCAD. For that, CADprompt opens a small server inside FreeCAD
  (the Model Context Protocol, over HTTP). The server listens only on this
  computer (`127.0.0.1`), on a port that the system picks. Each request needs a
  token that is made new for each start and is kept in memory only; the panel
  gives it to the Claude Code program that it starts. A web page cannot use the
  server: a request from a browser is refused. The server starts when you send
  a message to Claude and have allowed Claude Code, not when FreeCAD starts.
  It stops when you close the panel, choose a local model or an API key, take
  your yes for Claude Code back, or quit FreeCAD.
- Claude Code gets six tools from that server: `run_python`,
  `get_document_state`, `make_drawing`, `find_joints`, `make_weld` and
  `get_view`. Claude Code started by the panel cannot show a question of its
  own, so the question is asked in FreeCAD: `run_python`, `make_drawing` and
  `make_weld` wait for your Run or Skip in the panel, exactly as with a local
  model. The three tools that only look do not ask. CADprompt starts Claude
  Code in its asking mode and with three tools of its own, the ones that
  read (Read, Glob, Grep): it can read the files of the project, and it has
  no shell and no tool that writes a file. A file that you ask for (a
  script, a note) is saved with `run_python`, so you see the code and press
  Run. Claude Code waits for your answer for up to one day (`"timeout"` in
  the `.mcp.json` of the addon).
- With each message to Claude Code the panel sends the state of the project
  along, so that Claude Code does not have to ask for it first: the mode,
  the entries of `refs/INDEX.md`, the names of the files in `model/`, the
  FreeCAD version, and the description of the open document (object names
  and sizes).
- **A project folder from another person** cannot give Claude Code other
  tools. CADprompt tells Claude Code to use the server list of the addon and
  no other, so a `.mcp.json` in the project folder is not read by the panel.
  A project with hooks (commands that Claude Code runs by itself, in
  `.claude/settings.json`, `.claude/settings.local.json` or in the head of a
  command file) is not started: the panel names the file. CADprompt also tells
  Claude Code not to count a "trust this folder" answer that it stored for the
  folder. Its `CLAUDE.md` is read: look at it before you send a message in a
  project that is not yours.
- The tools work in the document that was active when you sent the message,
  also when you click another document while Claude works. After Stop, after
  New chat, and after Claude Code has ended, the server runs no more tool calls
  until your next message. When Claude Code gives a tool call up while its
  question waits for you, the question closes and the step is not run.
- If your computer uses a proxy (`HTTP_PROXY`, `HTTPS_PROXY`), CADprompt adds
  `127.0.0.1` and `localhost` to `NO_PROXY` for Claude Code, so the requests to
  FreeCAD stay on this computer.

## Training data: a file in the project, off until you turn it on

Settings has the check box **Save good requests as training data in the
project**. It is off in a new installation. Off means: no file is made, nothing
is written, and a file that is there already is left alone.

When it is on, each request to a local model or with an API key that ended
without a problem is added as one line to the file `training/requests.jsonl`
in the project folder. The file is there so that a small local model can learn
from real, good requests later. **Nothing is sent anywhere, and no connection
is made for it.** The file stays on this computer, and CADprompt only adds
lines to it. It is a text file: you can open it in an editor and delete it at
any time. The first time a request is saved in a project, the chat says so in
one line and names the file.

- **Which requests.** Only those that ended well: no error, not stopped, not
  cut off, the step limit not reached, no problem left in the model, no
  drawing that failed, no step that you answered with Skip, and the model was
  saved. A question that changed nothing is not written. A request in which a
  step failed and the AI repaired it is written whole, with the error and the
  repair.
- **What a line holds.** The time, the source (local model or API key), the
  name of the model, the mode (Build or Teach), the instructions that CADprompt
  gave the model, the names of the tools, and the messages of the request: your
  message, the code that the AI wrote, what the tools reported, and the final
  answer. When the request was a follow-up, the earlier messages that the model
  got are there too, marked as context.
- **What is never written.** A saved key, or the token of the server for Claude
  Code. The pixels of an image: only its path in the project. The text of a
  project file that the AI read with `read_project_file`: only its path and
  its length, because `refs/` can hold pages of a book. Claude's thinking
  blocks. The path of the project folder and of your home folder, which has
  your user name in it: the words `PROJECT` and `~` stand for them.
- **What is written as it is.** Your own words, the code of the AI and the
  answers of the AI. A name or an address that you type into a message is in
  the file. When the AI says in its own words what a file holds, that is in the
  file too; a long line that it copies from the file is taken out. Read the
  file before you give it to another person.
- **Claude Code.** Requests that Claude Code ran (the Claude account, or
  another endpoint) are not written.
- **Where the file is safe.** On Linux and macOS only you can read the file and
  its folder (modes 0600 and 0700). Windows has no such modes: there the file
  has the permissions of the project folder. The file is a part of the project
  folder: when you share or sync that folder, the file goes with it. CADprompt
  writes only inside the project: when `training` or the file is a link to
  another place, nothing is written, and the chat says so once. A file that
  cannot be written never stops a request.

## Every connection that CADprompt can make

| Connection | When | What is sent | What must be true first |
|---|---|---|---|
| To the model server of the source "Local model" (`POST <address>/chat/completions`) | once for each step of the AI after you send a message | the conversation of the project: your messages, the results of the AI's code, descriptions of the document (object names, sizes), the images that you attached, the text of project files that the AI read; the key of that server when you saved one | a server on this computer: nothing. Another computer: your Allow for that host |
| To the same server (`GET <address>/models`) | when you press Find models | the key of that server when you saved one | the same |
| To `api.anthropic.com` (`POST /v1/messages`), source "Anthropic API key" | once for each step of the AI after you send a message | the same conversation data, and your API key | your Allow for Anthropic |
| To `api.anthropic.com` (`GET /v1/models`) | when you press Find models | your API key | your Allow for Anthropic |
| To the proxy of your computer, when its settings name one | before each https connection above to another computer | the name of the service (`CONNECT api.anthropic.com:443`), and the login for the proxy when the setting has one | the same Allow as for the connection |
| The program `claude auth status` (it is started; CADprompt itself connects to nothing) | when the panel opens or the settings change, with a Claude Code source | nothing from CADprompt | your Allow for Claude Code |
| The program `claude -p` (Claude Code; it sends the data to Anthropic or to your endpoint with its own connections) | once for each message | your message, and with it the mode, the entries of `refs/INDEX.md`, the names of the model files and the description of the open document; through the tools: the results of the AI's code, descriptions of the document, pictures of the 3D view; and the project files that Claude Code reads | your Allow for Claude Code with that service |
| The server inside FreeCAD for Claude Code (`127.0.0.1`, incoming only) | listens from your first message to Claude until the panel closes | nothing leaves the computer | your Allow for Claude Code; each request needs the token |

CADprompt makes no other connection: no update check, no telemetry, no
download. FreeCAD's Addon Manager, not CADprompt, gets the updates. The
training data (above) is a file on this computer: it has no row here, because
nothing is sent.

## What is not done yet

- Claude Code is a program of its own. CADprompt asks you before it starts it,
  but it cannot see or limit the connections that Claude Code makes.
- The text of the `CLAUDE.md` and of the command files of a project folder is
  read by Claude Code. Look at them in a project that is not yours.
- Keys are files that only you can read, not entries in the key store of the
  system.
- No person has tested Windows and macOS.
- A fine-tuned small model for FreeCAD is planned.
