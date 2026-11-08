#!/usr/bin/env bash
# One command: build the plugin and lay the result out the way the Open MPC catalog's release.py expects.
#
#   release/build.sh
#
# Output (all under release/build/):
#   tidal-verb.so                      32-bit ARM (Force, MPC X/Live/One/Key)
#   aarch64/tidal-verb.so              64-bit ARM (MPC Live III, MPC XL), same file name
#   skin/Fullban - VST - Tidal Verb/   version.xml + Plugin Skins/
#   pluginlist-entry.xml                the plugin-list <PLUGIN/> element
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="Tidal Verb"
SLUG="tidal-verb"
UID_STR="FEwF"      # permanent: projects find the plugin by this
ZIGLANG_VERSION="0.17.1"

# zig (cross compiler) from the 'ziglang' Python package, pinned, in a private venv the SDK's build.py picks up
if [ ! -x .sdk-tools/zigenv/bin/python ]; then
  python3 -m venv .sdk-tools/zigenv
  .sdk-tools/zigenv/bin/python -m pip install --disable-pip-version-check --quiet "ziglang==$ZIGLANG_VERSION"
fi

python3 tools/build.py "plugins/$NAME"          # native + arm32 + arm64, validated

SRC="plugins/$NAME/build/Fullban - VST - $NAME"
B=release/build
rm -rf "$B"
mkdir -p "$B/aarch64" "$B/skin/Fullban - VST - $NAME"
cp "$SRC/VST/$SLUG-arm32.so" "$B/$SLUG.so"
cp "$SRC/VST/$SLUG-arm64.so" "$B/aarch64/$SLUG.so"
cp "$SRC/version.xml" "$B/skin/Fullban - VST - $NAME/version.xml"
cp -r "$SRC/Plugin Skins" "$B/skin/Fullban - VST - $NAME/Plugin Skins"

UIDHEX=$(python3 -c "import sys; print(format(int.from_bytes(sys.argv[1].encode(), 'big'), 'x'))" "$UID_STR")
cat > "$B/pluginlist-entry.xml" <<EOF
<PLUGIN name="$NAME" descriptiveName="$NAME" format="VST" category="Effect" manufacturer="Fullban" version="1.0" file="/sdcard/Synths/Fullban - VST - $NAME/$SLUG.so" uid="$UIDHEX" isInstrument="0" fileTime="0" infoUpdateTime="0" numInputs="2" numOutputs="2" isShell="0"/>
EOF
echo "built: $B"
