# cadprompt -- shared helpers. Sourced by bin/cadprompt, bin/cadprompt-grab and setup.
# shellcheck shell=bash
# shellcheck disable=SC2034  # settings are used by the scripts that source this

CAD_HOME="${CADPROMPT_HOME:-$HOME/Work/CAD}"                 # one folder per project
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/cadprompt"        # token
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/cadprompt"   # link to the active project
RPC_PORT=9875                                                 # freecad-mcp addon's port
FREECAD_MCP_VERSION="0.1.28"                                  # addon and MCP server must match
FLATPAK_ID="org.freecad.FreeCAD"

notify() { command -v notify-send >/dev/null 2>&1 && notify-send -a "CAD prompt" "$@" || true; }
# Scripts run from keybinds: nobody sees stderr, so errors go to a notification too.
die() { printf 'cadprompt: %s\n' "$*" >&2; notify -u critical "CAD prompt" "$*"; exit 1; }

# Which FreeCAD: "native" (freecad on PATH), "flatpak", or "none".
# CADPROMPT_FREECAD=flatpak forces the Flatpak when both are installed.
fc_kind() {
    if [[ ${CADPROMPT_FREECAD:-} == flatpak ]]; then echo flatpak
    elif command -v freecad >/dev/null 2>&1; then echo native
    elif command -v flatpak >/dev/null 2>&1 && flatpak info "$FLATPAK_ID" >/dev/null 2>&1; then echo flatpak
    else echo none
    fi
}

# The GUI, as an argv array in FC_GUI.
fc_gui() {
    case $(fc_kind) in
        native)  FC_GUI=(freecad) ;;
        flatpak) FC_GUI=(flatpak run "$FLATPAK_ID") ;;
        *)       return 1 ;;
    esac
}

# Headless FreeCAD as one command string (what freecad-mcp's --freecadcmd wants).
fc_cmd_string() {
    case $(fc_kind) in
        native)  command -v freecadcmd >/dev/null 2>&1 && printf 'freecadcmd' || printf 'FreeCADCmd' ;;
        flatpak) printf 'flatpak run --command=freecadcmd %s' "$FLATPAK_ID" ;;
        *)       return 1 ;;
    esac
}

# FreeCAD's per-user data folder (holds Mod/ and freecad_mcp_settings.json).
fc_user_dir() {
    local cmd out
    cmd=$(fc_cmd_string) || return 1
    # shellcheck disable=SC2086  # the command string is meant to split
    out=$($cmd -c 'import FreeCAD; print("UAD=" + FreeCAD.getUserAppDataDir())' 2>/dev/null | sed -n 's/^UAD=//p' | tail -n1)
    [[ -n $out ]] || return 1
    printf '%s' "${out%/}"
}

rpc_up() { (exec 3<>"/dev/tcp/127.0.0.1/$RPC_PORT") 2>/dev/null; }

launch() { setsid -f uwsm-app -- "$@" >/dev/null 2>&1; }   # -f: always fork, never block the caller

# Open a model in the running FreeCAD through the MCP bridge (so no second
# FreeCAD appears). Skips files that are already open.
fc_open() {
    local file code
    file=$(readlink -f "$1")
    code=$(printf 'import os, FreeCAD as App\np = "%s"\nif not any(os.path.realpath(d.FileName or "") == p for d in App.listDocuments().values()):\n    App.openDocument(p)\n' \
           "$(printf '%s' "$file" | sed 's/\\/\\\\/g; s/"/\\"/g')" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
    curl -s -m 20 -o /dev/null -X POST "127.0.0.1:$RPC_PORT/RPC2" \
        -H "Authorization: Bearer $(<"$CONF_DIR/token")" -H 'Content-Type: text/xml' \
        --data "<?xml version=\"1.0\"?><methodCall><methodName>execute_code</methodName><params><param><value><string>$code</string></value></param></params></methodCall>" \
        || notify "Could not open $(basename "$file") in FreeCAD" "Open it by hand, or ask Claude to."
}

# Start FreeCAD unless its MCP server already answers; wait until it does.
# A file argument is opened on start (FC_STARTED=1 then); if FreeCAD already
# runs, the caller opens it with fc_open instead.
ensure_freecad() {
    FC_STARTED=""
    rpc_up && return 0
    fc_gui || die "FreeCAD not found (pacman -S freecad, or the Flathub app)."
    FC_STARTED=1
    launch "${FC_GUI[@]}" "$@"
    local _
    for _ in $(seq 1 120); do
        rpc_up && return 0
        sleep 0.5
    done
    notify -u critical "FreeCAD is up, but its MCP server is not" \
        "In FreeCAD: MCP Addon workbench, FreeCAD MCP > Auto-Start Server."
    return 1
}

active_project() { [[ -L $STATE_DIR/active ]] && readlink -f "$STATE_DIR/active"; }
set_active_project() { mkdir -p "$STATE_DIR"; ln -sfn "$1" "$STATE_DIR/active"; }

slug() {
    local s
    s=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/[^a-z0-9._-]\+/-/g' -e 's/^-\+//' -e 's/-\+$//')
    printf '%s' "${s:-untitled}"
}
