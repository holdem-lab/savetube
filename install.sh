#!/usr/bin/env bash
# SaveTube installer — run once on a Mac:
#   curl -fsSL https://raw.githubusercontent.com/holdem-lab/savetube/main/install.sh | bash
#
# Installs dependencies (python-tk, yt-dlp, ffmpeg) via Homebrew, drops the
# app into ~/.savetube, and creates a double-clickable launcher on the Desktop.
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/holdem-lab/savetube/main"
APP_DIR="$HOME/.savetube"
LAUNCHER="$HOME/Desktop/SaveTube.command"

echo "==> SaveTube install"

# 1. Homebrew (package manager) — needed for yt-dlp/ffmpeg/python-tk.
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew не найден. Установи его командой и запусти этот скрипт снова:"
  echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi

echo "==> Ставлю зависимости (yt-dlp, ffmpeg, python-tk)…"
brew install yt-dlp ffmpeg python-tk

# 2. App code.
echo "==> Скачиваю приложение в $APP_DIR"
mkdir -p "$APP_DIR"
curl -fsSL "$REPO_RAW/savetube.py" -o "$APP_DIR/savetube.py"

# 3. Launcher — Python from brew has Tk; resolve it for the double-click path.
PYBIN="$(brew --prefix)/bin/python3"
[ -x "$PYBIN" ] || PYBIN="python3"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYBIN" "$APP_DIR/savetube.py"
EOF
chmod +x "$LAUNCHER"

echo ""
echo "✅ Готово. Запуск: двойной клик по SaveTube.command на Рабочем столе."
echo "   (Первый раз: правый клик → Открыть, чтобы обойти Gatekeeper.)"
