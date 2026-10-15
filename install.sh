#!/bin/sh
# Install whispering: live mic + speaker transcription via a local Lemonade server.
#   curl -fsSL https://raw.githubusercontent.com/jflaflamme/whispering/main/install.sh | sh
# A test branch: curl -fsSL .../whispering/<branch>/install.sh | WHISPERING_BRANCH=<branch> sh
set -eu

BRANCH="${WHISPERING_BRANCH:-main}"
REPO_RAW="${WHISPERING_RAW:-https://raw.githubusercontent.com/jflaflamme/whispering/$BRANCH}"
SHARE_DIR="${SHARE_DIR:-$HOME/.local/share/whispering}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
MODEL="Whisper-Large-v3-Turbo"
SERVER="http://localhost:13305/api/v1"

say() { printf '%s\n' "$*"; }
warn() { printf '! %s\n' "$*"; }

mkdir -p "$BIN_DIR"
say "Installing whispering to $BIN_DIR/whispering"
curl -fsSL "$REPO_RAW/whispering.py" -o "$BIN_DIR/whispering"
chmod +x "$BIN_DIR/whispering"
mkdir -p "$SHARE_DIR"
curl -fsSL "$REPO_RAW/khmer_server.py" -o "$SHARE_DIR/khmer_server.py"
chmod +x "$SHARE_DIR/khmer_server.py"
curl -fsSL "$REPO_RAW/speakers.py" -o "$SHARE_DIR/speakers.py"
chmod +x "$SHARE_DIR/speakers.py"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Add it, or run $BIN_DIR/whispering" ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (runs the script and its one dependency)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

command -v parec >/dev/null 2>&1 || warn "parec not found: sudo apt install pulseaudio-utils"

# Optional Claude Code skill (find, summarise and tidy transcripts). Asked on the terminal,
# since `curl | sh` uses stdin for this script; WHISPERING_SKILL=global|project|no skips the question.
ask() {  # ask "question" -> answer on stdout, empty when there is no terminal
  if (exec </dev/tty) 2>/dev/null; then
    printf '%s' "$1" >/dev/tty
    read -r answer </dev/tty || answer=""
    printf '%s' "$answer"
  fi
}
if [ -d "$HOME/.claude" ] || command -v claude >/dev/null 2>&1; then
  choice="${WHISPERING_SKILL:-}"
  if [ -z "$choice" ]; then
    say ""
    say "Claude Code skill: lets Claude find, summarise and clean up your transcripts."
    choice=$(ask "Install it? [g] all projects (~/.claude/skills)  [p] this folder only ($PWD)  [n] no (default): ")
  fi
  case "$choice" in
    g|global) SKILL_DIR="$HOME/.claude/skills/whispering" ;;
    p|project) SKILL_DIR="$PWD/.claude/skills/whispering" ;;
    *) SKILL_DIR="" ;;
  esac
  if [ -n "$SKILL_DIR" ] && [ -e "$SKILL_DIR/SKILL.md" ]; then
    case "$(ask "A whispering skill is already in $SKILL_DIR. Replace it? [y/N]: ")" in
      y|Y|yes) ;;
      *) say "Kept the existing skill in $SKILL_DIR"; SKILL_DIR="" ;;
    esac
  fi
  if [ -n "$SKILL_DIR" ]; then
    mkdir -p "$SKILL_DIR/scripts"
    for f in SKILL.md scripts/sessions.py scripts/housekeeping.py; do
      curl -fsSL "$REPO_RAW/skill/whispering/$f" -o "$SKILL_DIR/$f"
    done
    chmod +x "$SKILL_DIR"/scripts/*.py
    say "Installed the Claude Code skill in $SKILL_DIR"
  fi
fi

if ! command -v lemonade >/dev/null 2>&1; then
  warn "Lemonade is not installed. Install it from the PPA, then run this installer again:"
  say  "    sudo add-apt-repository ppa:lemonade-team/stable"
  say  "    sudo apt update && sudo apt install lemonade-server"
  exit 0
fi

if ! curl -fsS "$SERVER/health" >/dev/null 2>&1; then
  warn "Lemonade is installed but not answering on $SERVER."
  say  "    sudo systemctl restart lemond   # then: lemonade status"
  exit 0
fi

say "Lemonade: $(lemonade --version 2>/dev/null || echo unknown version)"

if curl -fsS "$SERVER/models" | python3 -c "import json,sys; sys.exit(0 if any(m['id']=='$MODEL' and m.get('downloaded') for m in json.load(sys.stdin)['data']) else 1)"; then
  say "$MODEL is already downloaded"
else
  say "Downloading $MODEL (about 1.5 GB)"
  lemonade pull "$MODEL"
fi

if pgrep -af whisper-server 2>/dev/null | grep -q -- '--vad'; then
  say "Silero voice detection is on"
else
  say ""
  say "Recommended: turn on Silero voice detection so silence isn't transcribed as \"Thank you.\""
  say "See 'Stop invented text in silence' in https://github.com/jflaflamme/whispering#readme"
fi

say ""
say "Done. Run:  whispering        (Ctrl+C to stop, transcripts go to ~/transcripts/)"
say "Khmer:     whispering --language km   (first run downloads the Khmer model, about 2 GB)"
