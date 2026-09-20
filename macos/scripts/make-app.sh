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
# SIGN_IDENTITY selects the codesigning identity; it defaults to `-`, an ad-hoc signature.
# TCC keys a grant on the bundle id *and* the signature, so re-signing ad-hoc after signing
# with a real identity silently throws the grant away and the app goes back to being
# prompted — or, worse, quietly refused. Pass the identity you signed with before:
#   SIGN_IDENTITY="VNR Dev" PRODUCT=VNRCapture ./scripts/make-app.sh run /tmp/capture.wav 6
#
# The identity is remembered in dist/.sign-identity and a change is reported, because the
# consequence of a change is invisible until a recording comes back silent.
#
# Note: `security find-identity -v -p codesigning` can report 0 valid identities on a
# machine where `codesign --sign "VNR Dev"` works perfectly well. So nothing here consults
# it — codesign itself is the only authority on whether an identity can sign.
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
SIGN_IDENTITY="${SIGN_IDENTITY:--}"
IDENTITY_RECORD="dist/.sign-identity"

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

# TCC identifies an app by bundle id *and* signature, so an unsigned bundle can be
# refused outright and a changed identity invalidates an existing grant.
if [[ "${SIGN_IDENTITY}" == "-" ]]; then
    echo "Signing (ad-hoc)…"
else
    echo "Signing as ${SIGN_IDENTITY}…"
fi
# Failure is printed, not swallowed: a hidden codesign error reads downstream as a
# microphone that simply does not work.
if ! CODESIGN_OUTPUT="$(codesign --force --sign "${SIGN_IDENTITY}" --timestamp=none "${APP}" 2>&1)"; then
    echo "error: codesign failed with identity '${SIGN_IDENTITY}'" >&2
    [[ -n "${CODESIGN_OUTPUT}" ]] && echo "${CODESIGN_OUTPUT}" >&2
    echo "The microphone prompt will not appear for an unsigned bundle." >&2
    exit 1
fi

# A changed identity means the existing grant no longer applies to this bundle. That shows
# up as silence rather than as an error, so say it out loud.
PREVIOUS_IDENTITY=""
[[ -f "${IDENTITY_RECORD}" ]] && PREVIOUS_IDENTITY="$(cat "${IDENTITY_RECORD}")"
if [[ -n "${PREVIOUS_IDENTITY}" && "${PREVIOUS_IDENTITY}" != "${SIGN_IDENTITY}" ]]; then
    echo "warning: signing identity changed ('${PREVIOUS_IDENTITY}' -> '${SIGN_IDENTITY}')." >&2
    echo "         The microphone grant was tied to the old signature and no longer" >&2
    echo "         applies. Expect a fresh prompt, and run 'make-app.sh reset' if none" >&2
    echo "         appears. Set SIGN_IDENTITY='${PREVIOUS_IDENTITY}' to keep the grant." >&2
fi
printf '%s' "${SIGN_IDENTITY}" > "${IDENTITY_RECORD}"

echo "Built ${APP}"
echo "  bundle id:  ${BUNDLE_ID}"
echo "  executable: ${PRODUCT}"
echo "  signed by:  ${SIGN_IDENTITY}"

if [[ "${1:-}" != "run" ]]; then
    echo
    echo "Run it with:  ./scripts/make-app.sh run"
    exit 0
fi

# Each tool writes its own log, named after the product.
case "${PRODUCT}" in
    VNRCapture) LOG="${TMPDIR:-/tmp}/vnr-capture.log" ;;
    VNRApp)     LOG="" ;;   # a resident menu-bar app; it has no run to report
    *)          LOG="${TMPDIR:-/tmp}/vnr-probe.log" ;;
esac
[[ -n "${LOG}" ]] && rm -f "${LOG}"

shift || true   # drop "run"; whatever remains is passed to the app

echo
echo "Launching…  (answer the permission dialog if one appears)"
# `open` rather than executing the binary directly: launched from a shell, the terminal
# becomes the responsible process for TCC and the grant lands on Terminal instead of on
# this app. -W waits for the app to exit so the log is complete.
#
# The status is captured rather than allowed to trip `set -e`: the app exits non-zero when
# it fails *or* when it captured with warnings, and in both cases the log is the thing
# worth reading. Aborting here would throw away the only explanation.
#
# VNRApp is a menu-bar app: it stays resident, so `-W` would wait for the user to quit.
# The others are one-shot tools whose log is the whole point of running them.
LAUNCH_STATUS=0
WAIT_FLAG="-W"
[[ "${PRODUCT}" == "VNRApp" ]] && WAIT_FLAG=""

if [[ $# -gt 0 ]]; then
    open ${WAIT_FLAG} "${APP}" --args "$@" || LAUNCH_STATUS=$?
else
    open ${WAIT_FLAG} "${APP}" || LAUNCH_STATUS=$?
fi

if [[ -z "${LOG}" ]]; then
    echo
    echo "${PRODUCT} is running in the menu bar. Press ⌃⌥Space to record."
    echo "Quit it from the menu-bar item when you are done."
    exit "${LAUNCH_STATUS}"
fi

echo
if [[ -f "${LOG}" ]]; then
    cat "${LOG}"
else
    echo "No log at ${LOG} — the app may have failed to launch." >&2
    if [[ "${LAUNCH_STATUS}" -ne 0 ]]; then
        echo "open exited ${LAUNCH_STATUS}." >&2
    fi
    echo "Try running the binary directly to see the error:" >&2
    echo "  ${APP}/Contents/MacOS/${PRODUCT}" >&2
    exit 1
fi
