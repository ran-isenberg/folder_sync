#!/bin/bash
set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║       FolderSync  Builder             ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Check Homebrew ──────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "❌  Homebrew not found. Install it first: https://brew.sh"
  exit 1
fi

# ── 2. Check / install rclone ──────────────────────────────────────────
if ! command -v rclone &>/dev/null; then
  echo "📦  Installing rclone..."
  brew install rclone
else
  echo "✅  rclone already installed"
fi

# ── 3. Check / install Python 3 ───────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "📦  Installing Python 3..."
  brew install python
else
  echo "✅  Python 3: $(python3 --version)"
fi

# ── 4. Check / install uv ─────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
  echo "📦  Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
else
  echo "✅  uv already installed"
fi

# ── 5. Install Python dependencies ───────────────────────────────────
echo ""
echo "📦  Installing Python packages with uv..."
uv sync


# ── 7. Check app icon ────────────────────────────────────────────────
if [ -f "FolderSync.icns" ]; then
  echo "✅  Icon found"
else
  echo "⚠️  FolderSync.icns not found — build will use default icon"
fi

# ── 8. Stamp build info and build the .app ───────────────────────────
echo ""
echo "🔨  Building FolderSync.app..."

BUILD_TIME=$(date '+%Y-%m-%d %H:%M')
BUILD_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

# Stamp sync.py with build time and git hash (restored after build)
cp sync.py sync.py.bak
sed -i '' "s/^APP_BUILD_TIME = .*/APP_BUILD_TIME = '$BUILD_TIME'/" sync.py
sed -i '' "s/^APP_BUILD_HASH = .*/APP_BUILD_HASH = '$BUILD_HASH'/" sync.py

uv run pyinstaller FolderSync.spec --noconfirm --clean

# Restore original sync.py
mv sync.py.bak sync.py

APP_SRC="dist/FolderSync.app"

if [ ! -d "$APP_SRC" ]; then
  echo "❌  Build failed — dist/FolderSync.app not found"
  exit 1
fi

echo "✅  App built successfully"

# ── 9. Bundle rclone binary into the app ──────────────────────────────
echo ""
echo "📦  Bundling rclone into app..."

RCLONE_PATH="$(command -v rclone)"
if [ -n "$RCLONE_PATH" ]; then
  RCLONE_DEST="$APP_SRC/Contents/Resources/rclone"
  cp "$RCLONE_PATH" "$RCLONE_DEST"
  chmod +x "$RCLONE_DEST"
  echo "✅  rclone bundled from $RCLONE_PATH"
else
  echo "⚠️  rclone not found — app will require rclone to be installed separately"
fi

# ── 10. Build the DMG ─────────────────────────────────────────────────
echo ""
echo "💿  Building FolderSync.dmg..."

DMG_OUT="FolderSync.dmg"
DMG_TMP="FolderSync_tmp.dmg"
DMG_STAGE=$(mktemp -d)

[ -f "$DMG_OUT" ] && rm "$DMG_OUT"
[ -f "$DMG_TMP" ] && rm "$DMG_TMP"

# Stage: copy app + symlink to /Applications
cp -r "$APP_SRC" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"

# Create DMG with hdiutil (no Finder AppleScript, never hangs)
hdiutil create -volname "FolderSync" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG_OUT" \
  && echo "✅  FolderSync.dmg created" \
  || echo "⚠️  DMG not created — continuing with direct install"

rm -rf "$DMG_STAGE"

# ── 11. Install & Launch (only with --install flag) ───────────────────
if [ "${1:-}" = "--install" ]; then
  echo ""
  APP_DST="/Applications/FolderSync.app"

  if [ -d "$APP_DST" ]; then
    echo "🗑   Removing old version..."
    rm -rf "$APP_DST"
  fi

  cp -r "$APP_SRC" "/Applications/"
  echo "✅  Installed to /Applications"

  # Strip quarantine so Gatekeeper doesn't block first launch
  find "/Applications/FolderSync.app" -exec xattr -d com.apple.quarantine {} \; 2>/dev/null || true

  echo ""
  echo "🚀  Launching FolderSync..."
  open "/Applications/FolderSync.app"

  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  ✅  Done!                                                    ║"
  echo "║                                                              ║"
  echo "║  • App running — look for ☁️  in your menu bar (top right)   ║"
  echo "║  • FolderSync.dmg available in project directory              ║"
  echo "║  • Click ☁️  → Configure to set your NAS path               ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
else
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  ✅  Build complete!                                         ║"
  echo "║                                                              ║"
  echo "║  • dist/FolderSync.app ready                                 ║"
  echo "║  • FolderSync.dmg ready (if create-dmg succeeded)            ║"
  echo "║                                                              ║"
  echo "║  Run './build.sh --install' to install & launch              ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
fi
