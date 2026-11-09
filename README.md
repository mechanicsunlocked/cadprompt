# CAD prompt

Press **SUPER+ALT+C**, name a project, say what you want. A Claude Code
window opens tiled next to FreeCAD and the part appears there, feature by
feature, parametric, with its sizes in one `Params` set. Keep talking to change
it, or take over in FreeCAD.

| Key | Does |
|---|---|
| SUPER+ALT+C | prompt (build mode) |
| SUPER+ALT+SHIFT+C | prompt in teach mode: one feature at a time, which tool and why |
| SUPER+ALT+R | screenshot a region into the project (a photo, a catalogue drawing) |
| SUPER+ALT+SHIFT+R | newest file in Downloads into the project (that PDF you just saved) |
| SUPER+ALT+V | clipboard into the project (image, copied files, or a link) |

**Bar icon** (a cube, right section): click for the projects menu, right
click for the prompt. The menu is Omarchy's own overlay: every project with
its mode, age, model count and the last thing you asked Claude there as
subtext. Pick one to open it; rows for a new project, for deleting one
(folder and conversation, after a confirm), and for the full projects window
(`cadprompt-tui`, a gum TUI with the same rows).

In the prompt: `/teach`, `/build`, `/refs` (what is in the grabbed material),
`/check` (review before the shop), `/drawing A3` (DIN workshop drawing, Hoischen
style, PDF in `drawings/`).

Each project is a folder in `~/Work/CAD`: `model/` (the .FCStd), `refs/` (what you
grabbed), `drawings/` (PDFs). Reopening a project continues its conversation
and opens its newest model in FreeCAD, in the running one if there is one.

## To the workshop tablet

The cube menu has "Send newest drawing to tablet". It takes the newest PDF in
the active project's `drawings/` and sends it over Taildrop, Tailscale's own
file drop: laptop to tablet directly, encrypted, nothing in between. The first
time, it asks which tailnet machine is the tablet and remembers it in
`~/.config/cadprompt/tablet` (`cadprompt-send --tablet` to change). On the
tablet, Tailscale must be running; the file lands in its Downloads and opens in
any PDF reader. If the tablet is asleep or off the tailnet, you get a
"could not send" notification; send again when it is awake. Also:
`cadprompt-send some.pdf other.pdf`.

## Install, update, remove

Needs Omarchy 4, FreeCAD 1.1 (`pacman -S freecad` or the Flathub app), Claude
Code (logged in), `uv`, `jq`, `git`, and for the grab keys `grim slurp
wl-clipboard`.

```bash
curl -fsSL https://raw.githubusercontent.com/mechanicsunlocked/cadprompt/main/install | bash
```

That clones into `~/Work/cadprompt` and installs. **The same line updates**:
run it again and it pulls the newest version and reinstalls. From inside the
folder, `./setup update` does the same, `./setup install` reinstalls without
pulling, and:

```bash
./setup remove           # takes every link and line away; --purge also token and vendor/
```

Nothing is copied into the system. `setup` makes symlinks into this folder
(three in `~/.local/bin`, one in `~/.config/hypr`, two in FreeCAD's `Mod`),
appends one `require` line to `~/.config/hypr/hyprland.lua`, adds one
command-module entry to the bar in `~/.config/omarchy/shell.json`, writes a
token to `~/.config/cadprompt/`, and sets FreeCAD's MCP addon to localhost
plus token. `remove` undoes exactly that. Move or delete this folder after `remove`
and nothing is left. `~/Work/CAD` is yours and never touched. No bar widget, no
desktop files, no daemons.

## What runs, what it can reach

```
key -> cadprompt -> foot, tiled: Claude Code in ~/Work/CAD/<project>
                        | .mcp.json: uvx freecad-mcp==0.1.28 (token from env)
                        v
        FreeCAD + freecad-mcp addon (127.0.0.1:9875, token required)
        + Hoischen addon for TechDraw
```

Claude in a project may: use the FreeCAD tools, and read and write inside
the project. Reads outside it are refused, not just denied by rule. It never
sees `~/Downloads`: the grab keys copy what you pick into `refs/`. No shell. WebSearch and WebFetch ask first. It cannot edit its
own `.claude/`, `.mcp.json` or `CLAUDE.md`.

**Security, the honest version.** `execute_code` runs Python inside FreeCAD,
as your user. CLAUDE.md tells Claude to keep it to FreeCAD; that is an
instruction, not a wall. The wall is a sandboxed FreeCAD. With the Flatpak:

```bash
flatpak override --user --nofilesystem=home --filesystem=~/Work/CAD org.freecad.FreeCAD
```

Then FreeCAD, and anything Claude runs inside it, sees `~/Work/CAD` and nothing
else of your home. Network stays shared (the MCP bridge needs localhost).
With the Arch package there is no such wall; keep homelab keys out of this
machine's home or use the Flatpak.

The bridge listens on localhost only and answers 401 without the token, so a
web page in your browser cannot post code to FreeCAD.

**Privacy.** What leaves the machine: your prompts, the project files Claude
reads, and FreeCAD screenshots when Claude asks for one (to Anthropic, via
Claude Code). At install, `git` fetches freecad-mcp from GitHub and `uvx`
fetches it from PyPI. Nothing else phones anywhere. No telemetry of its own.

## Updates

- **cadprompt itself**: the install line above, again. Or `./setup update`.
- **freecad-mcp** (the bridge inside FreeCAD): pinned to one version in
  `lib/common.sh`; `./setup upgrade` moves to its newest release, prints the
  GitHub compare link for the jump, then asks you to restart FreeCAD.
  `./setup upgrade 0.1.28` pins a specific one, e.g. to go back.
- Nothing updates by itself. Claude Code and `uv` update through their own
  channels; the Hoischen addon ships in this repo.

## Layout

```
install               the one-line installer (clone or pull, then setup install)
setup                 install | update | upgrade | remove
bin/cadprompt         launcher: picker, project from template, FreeCAD, terminal
bin/cadprompt-grab    region | clipboard | last-download | FILE...
bin/cadprompt-tui     projects window and the bar menu: open, new, delete, send
bin/cadprompt-send    newest drawing to the tablet over Taildrop
lib/common.sh         FreeCAD detection, RPC check, active project
hypr/cadprompt.lua    the five keys (Omarchy Lua config)
template/             copied into every new project
freecad-hoischen/     TechDraw addon: DIN preset, dimension lines at 10 / 7 mm
vendor/               freecad-mcp checkout, created by setup
```

## Checks after install

1. `hyprctl configerrors` empty; `omarchy menu keybindings --print | grep CAD`
   shows five lines.
2. `cadprompt test1`: a tiled window titled `CAD · test1`, class `cadprompt`
   (`hyprctl clients`), FreeCAD starts. Trust the folder when asked; `/mcp`
   shows `freecad` connected.
3. "a 100 x 50 x 10 plate with four 8 mm holes, 10 mm from the edges" appears
   live, driven by `Params`, saved in `model/`. If calls hang, FreeCAD has a
   dialog open; close it.
4. `curl -s -o /dev/null -w '%{http_code}\n' -X POST 127.0.0.1:9875/RPC2` prints 401.
5. In the prompt, ask Claude to read `~/.bashrc` (a harmless file outside the
   project). Claude Code itself must refuse, before the model gets to decide.
