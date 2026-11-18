#!/usr/bin/env bash
# One-time setup for the `stems` command on macOS.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"

[ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
if ! command -v brew >/dev/null; then
  echo "Homebrew is required: https://brew.sh"; exit 1
fi
command -v ffmpeg >/dev/null || brew install ffmpeg
command -v uv >/dev/null || brew install uv

echo "Creating Python environment (downloads PyTorch + audio-separator, ~1 GB)..."
uv sync

mkdir -p "$HOME/.local/bin"
for cmd in stems stemsd; do
  cat > "$HOME/.local/bin/$cmd" <<LAUNCH
#!/usr/bin/env bash
exec "$HERE/.venv/bin/$cmd" "\$@"
LAUNCH
  chmod +x "$HOME/.local/bin/$cmd"
done

case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
     echo "Added ~/.local/bin to PATH in ~/.zshrc (open a new terminal)." ;;
esac

echo
echo "Installed. Try:  stems \"https://www.youtube.com/watch?v=...\""
