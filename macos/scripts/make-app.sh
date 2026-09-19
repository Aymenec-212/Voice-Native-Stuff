#!/usr/bin/env bash
# Wrap a SwiftPM executable in a minimal .app bundle.
#
# A bare SwiftPM binary has no Info.plist, so it has no NSMicrophoneUsageDescription and
# no bundle identifier. macOS then refuses to grant it a microphone: it either hands the
# process digital silence or kills it when access is requested. Neither reads as a
# permission problem. This script produces the bundle that makes the prompt possible.
#
#   ./scripts/make-app.sh              build and bundle
#   ./scripts/make-app.sh run [args…]  build, bundle, launch with args, print the log
#   ./scripts/make-app.sh reset        forget the microphone grant, so the prompt returns
#
# PRODUCT=VNRProbe by default; set it to bundle a different executable target:
#   PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 6
#
# The bundle identifier does not change with PRODUCT, so every tool here shares one
# microphone grant — the probe asks for it, the others inherit it.
set -euo pipefail

cd "$(dirname "$0")/.."

PRODUCT="${PRODUCT:-VNRProbe}"
APP_NAME="${APP_NAME:-VoiceNativeResearch}"
CONFIGURATION="${CONFIGURATION:-debug}"
BUNDLE_ID="com.aymenec.voicenativeresearch"
APP="dist/${APP_NAME}.app"

reset_permission() {
    echo "Resetting the microphone grant for ${BUNDLE_ID}…"
    # Non-fatal: it fails when no grant exists yet, which is fine.
    tccutil reset Microphone "${BUNDLE_ID}" || true
    echo "Next launch will prompt again."
}

if [[ "${1:-}" == "reset" ]]; then
    reset_permission
    exit 0
fi

echo "Building ${PRODUCT} (${CONFIGURATION})…"
swift build -c "${CONFIGURATION}" --product "${PRODUCT}"
BINARY="$(swift build -c "${CONFIGURATION}" --product "${PRODUCT}" --show-bin-path)/${PRODUCT}"
[[ -x "${BINARY}" ]] || { echo "error: ${BINARY} was not produced" >&2; exit 1; }

echo "Assembling ${APP}…"
rm -rf "${APP}"
mkdir -p "${APP}/Contents/MacOS" "${APP}/Contents/Resources"
cp "${BINARY}" "${APP}/Contents/MacOS/${PRODUCT}"
sed "s/__EXECUTABLE__/${PRODUCT}/" Resources/Info.plist > "${APP}/Contents/Info.plist"

# Ad-hoc signature. TCC identifies an app by bundle id *and* signature, so an unsigned
# bundle can be refused outright, and changing the signing identity invalidates an
# existing grant. Ad-hoc is stable enough for local development.
echo "Signing (ad-hoc)…"
codesign --force --sign - --timestamp=none "${APP}" >/dev/null 2>&1 \
    || echo "warning: codesign failed; the prompt may not appear" >&2

echo "Built ${APP}"
echo "  bundle id:  ${BUNDLE_ID}"
echo "  executable: ${PRODUCT}"

if [[ "${1:-}" != "run" ]]; then
    echo
    echo "Run it with:  ./scripts/make-app.sh run"
    exit 0
fi

# Each tool writes its own log, named after the product.
case "${PRODUCT}" in
    VNRCapture) LOG="${TMPDIR:-/tmp}/vnr-capture.log" ;;
    *)          LOG="${TMPDIR:-/tmp}/vnr-probe.log" ;;
esac
rm -f "${LOG}"

shift || true   # drop "run"; whatever remains is passed to the app

echo
echo "Launching…  (answer the permission dialog if one appears)"
# `open` rather than executing the binary directly: launched from a shell, the terminal
# becomes the responsible process for TCC and the grant lands on Terminal instead of on
# this app. -W waits for the app to exit so the log is complete.
if [[ $# -gt 0 ]]; then
    open -W "${APP}" --args "$@"
else
    open -W "${APP}"
fi

echo
if [[ -f "${LOG}" ]]; then
    cat "${LOG}"
else
    echo "No log at ${LOG} — the app may have failed to launch." >&2
    echo "Try running the binary directly to see the error:" >&2
    echo "  ${APP}/Contents/MacOS/${PRODUCT}" >&2
    exit 1
fi
