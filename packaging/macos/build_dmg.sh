#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: build_dmg.sh APP_PATH OUTPUT_DMG" >&2
    exit 2
fi

app=$1
output=$2
if [ ! -d "$app" ]; then
    echo "Missing app bundle: $app" >&2
    exit 1
fi

mkdir -p "$(dirname "$output")"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM
ditto "$app" "$stage/IBKR Dashboard.app"
cp LICENSE THIRD_PARTY_NOTICES.md "$stage/"
ln -s /Applications "$stage/Applications"
hdiutil create -volname "IBKR Dashboard" -srcfolder "$stage" -format UDZO -ov "$output"
