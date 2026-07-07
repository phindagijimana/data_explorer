#!/usr/bin/env bash
# Codesign + notarize dist/BIDSHub.app with a RESILIENT notarization wait.
#
# Same as sign_macos.sh but submits the notarization and then polls in a loop
# (instead of `notarytool submit --wait`, whose single long-lived connection can
# time out during an Apple-side queue stall even though the submission succeeds).
# The poll survives multi-hour queues and only stops on Accepted / Invalid.
#
#   DEVELOPER_ID_APP   e.g. "Developer ID Application: NAME (TEAMID)"
#   NOTARY_PROFILE     an `xcrun notarytool store-credentials` keychain profile
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DEVELOPER_ID_APP:?set DEVELOPER_ID_APP}"
: "${NOTARY_PROFILE:?set NOTARY_PROFILE}"
APP="dist/BIDSHub.app"
DMG="dist/BIDSHub.dmg"
ENTITLEMENTS="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"

echo "==> Codesigning (hardened runtime, deep, with entitlements)"
codesign --force --deep --options runtime --timestamp \
  --entitlements "$ENTITLEMENTS" \
  --sign "$DEVELOPER_ID_APP" "$APP"
codesign --verify --strict "$APP"

echo "==> Building signed .dmg"
packaging/make_dmg.sh
codesign --force --timestamp --sign "$DEVELOPER_ID_APP" "$DMG"

echo "==> Submitting for notarization"
SUBMIT_JSON="$(xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --output-format json)"
echo "$SUBMIT_JSON"
SUBMISSION_ID="$(echo "$SUBMIT_JSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')"
echo "==> Submission id: $SUBMISSION_ID"

echo "==> Polling notarization status (resilient; every 60s)"
STATUS="In Progress"
while [ "$STATUS" = "In Progress" ]; do
  sleep 60
  INFO="$(xcrun notarytool info "$SUBMISSION_ID" --keychain-profile "$NOTARY_PROFILE" --output-format json 2>/dev/null || echo '{}')"
  STATUS="$(echo "$INFO" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status","In Progress"))' 2>/dev/null || echo 'In Progress')"
  echo "[$(date +%H:%M:%S)] status=$STATUS"
done

if [ "$STATUS" != "Accepted" ]; then
  echo "==> Notarization did NOT pass (status=$STATUS). Fetching log:"
  xcrun notarytool log "$SUBMISSION_ID" --keychain-profile "$NOTARY_PROFILE" || true
  exit 2
fi

echo "==> Accepted — stapling"
xcrun stapler staple "$APP"
xcrun stapler staple "$DMG"
echo "==> Signed + notarized + stapled: $DMG"
