#!/usr/bin/env bash
# Build the iPhone app (Release, automatic signing with the team in project.yml) and install it on Eli's iPhone,
# over Wi-Fi or cable, with devicectl. Installing over an existing copy is an update: nothing on the phone is lost.
# Only needed when ios/ changes: the app shows the server's own page, so server deploys reach it by themselves.
#   ios/build-ios.sh               build, install, and open it on the phone
#   ios/build-ios.sh --no-install  build only
#   ios/build-ios.sh --sim         build for the simulator, run it there and save a screenshot (build/sim.png)
set -euo pipefail
cd "$(dirname "$0")"
MODE=phone
for a in "$@"; do case "$a" in --no-install) MODE=build;; --sim) MODE=sim;; esac; done
BUNDLE=com.elityre.sleepbandit
NUMBER="$(git rev-list --count HEAD)"          # the build number on the phone says which commit it has
xcodegen generate --quiet
mkdir -p build

if [ "$MODE" = sim ]; then
  SIM="${SB_SIMULATOR:-iPhone 17}"
  xcodebuild -project SleepBandit.xcodeproj -scheme SleepBandit -configuration Debug \
    -destination "platform=iOS Simulator,name=$SIM" -derivedDataPath build CURRENT_PROJECT_VERSION="$NUMBER" build 2>&1 \
    | grep -E "error:|warning: unre|BUILD" || true
  APP=build/Build/Products/Debug-iphonesimulator/SleepBandit.app
  [ -d "$APP" ] || { echo "build failed"; exit 1; }
  xcrun simctl boot "$SIM" 2>/dev/null || true
  xcrun simctl install "$SIM" "$APP"
  xcrun simctl launch "$SIM" "$BUNDLE" >/dev/null
  sleep "${SB_SIM_WAIT:-8}"
  xcrun simctl io "$SIM" screenshot build/sim.png >/dev/null 2>&1 && echo "screenshot: $(pwd)/build/sim.png"
  exit 0
fi

OUT="$(xcodebuild -project SleepBandit.xcodeproj -scheme SleepBandit -configuration Release -destination 'generic/platform=iOS' \
  -derivedDataPath build -allowProvisioningUpdates -allowProvisioningDeviceRegistration \
  CURRENT_PROJECT_VERSION="$NUMBER" build 2>&1 | grep -E "error:|BUILD" || true)"
echo "$OUT"
APP=build/Build/Products/Release-iphoneos/SleepBandit.app
case "$OUT" in *"BUILD SUCCEEDED"*) ;; *) echo "build failed"; exit 1;; esac
echo "built $APP (build $NUMBER)"
[ "$MODE" = phone ] || exit 0
DEVICE="${SB_IPHONE_UDID:-$(xcrun devicectl list devices 2>/dev/null | grep physical | grep -o '[0-9A-F]\{8\}-[0-9A-F]\{16\}' | head -1)}"
[ -n "$DEVICE" ] || { echo "no paired iPhone found (xcrun devicectl list devices)"; exit 1; }
echo "installing on $DEVICE ..."
xcrun devicectl device install app --device "$DEVICE" "$APP" 2>&1 | grep -E "installed|error|Error|unavailable|App is|bundleID" | head -5
xcrun devicectl device process launch --device "$DEVICE" "$BUNDLE" 2>&1 | grep -E "Launched|error|Error|locked" | head -3 || true
