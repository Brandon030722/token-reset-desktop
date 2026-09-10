#!/bin/bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="$(command -v python3.12)"
BUILD_DIR="$PROJECT_DIR/.local/native-build"
APP_DIR="$PROJECT_DIR/artifacts/Token重置.app"
mkdir -p "$BUILD_DIR" "$PROJECT_DIR/artifacts"
npm run build
if [ ! -x "$PROJECT_DIR/.local/package312/bin/python" ]; then
  "$PYTHON_BIN" -m venv "$PROJECT_DIR/.local/package312"
fi
BUILD_PYTHON="$PROJECT_DIR/.local/package312/bin/python"
"$BUILD_PYTHON" -m pip install 'pyinstaller==6.16.0'
"$BUILD_PYTHON" -m PyInstaller --noconfirm --onedir --console \
  --name TiboMonitorHelper --distpath "$BUILD_DIR/worker" \
  --workpath "$BUILD_DIR/work" --specpath "$BUILD_DIR" \
  --paths "$PROJECT_DIR" "$PROJECT_DIR/desktop/monitor_entry.py"
STAGING_APP="$BUILD_DIR/TiboMonitor.app"
if [ -d "$STAGING_APP" ]; then
  mv "$STAGING_APP" "$BUILD_DIR/previous-staging-$(date +%s)"
fi
mkdir -p "$STAGING_APP/Contents/MacOS" "$STAGING_APP/Contents/Resources"
swiftc -O -whole-module-optimization -swift-version 5 -parse-as-library \
  -target arm64-apple-macosx13.0 -framework AppKit -framework WebKit -framework UserNotifications \
  "$PROJECT_DIR/desktop/macos/TiboMonitor.swift" "$PROJECT_DIR/desktop/macos/EmailAccess.swift" \
  -o "$STAGING_APP/Contents/MacOS/TiboMonitor"
cp -R "$BUILD_DIR/worker/TiboMonitorHelper" "$STAGING_APP/Contents/Resources/monitor"
cp -R "$PROJECT_DIR/dist" "$STAGING_APP/Contents/Resources/site"
cp "$PROJECT_DIR/desktop/assets/AppIcon.icns" "$STAGING_APP/Contents/Resources/AppIcon.icns"
cp "$PROJECT_DIR/LICENSE" "$STAGING_APP/Contents/Resources/LICENSE"
STAGING_APP="$STAGING_APP" "$BUILD_PYTHON" - <<'PY'
import os, plistlib, json
from pathlib import Path
root = Path(os.environ["STAGING_APP"])
version = json.loads(Path("package.json").read_text())["version"]
with (root / "Contents/Info.plist").open("wb") as f:
    plistlib.dump({
        "CFBundleName": "Token重置", "CFBundleDisplayName": "Token重置",
        "CFBundleIdentifier": "org.tibo-reset.observatory",
        "CFBundleExecutable": "TiboMonitor", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version, "CFBundleVersion": "12",
        "LSUIElement": True,
        "CFBundleIconFile": "AppIcon", "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "© 2026 Tibo Reset Observatory contributors",
    }, f)
PY
codesign --force --deep --sign - "$STAGING_APP"
if [ -d "$APP_DIR" ]; then
  mv "$APP_DIR" "$BUILD_DIR/previous-release-$(date +%s).app"
fi
mv "$STAGING_APP" "$APP_DIR"
ditto -c -k --sequesterRsrc --keepParent "$APP_DIR" "$PROJECT_DIR/artifacts/token-reset-macOS-arm64.zip"
echo "Built: $APP_DIR"
