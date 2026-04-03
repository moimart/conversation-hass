#!/usr/bin/env bash
set -euo pipefail

echo "=== HAL Command — Install ==="

# Build
echo "Building..."
cargo build --release

# Install binary
BINDIR="${HOME}/.local/bin"
mkdir -p "$BINDIR"
cp target/release/hal-command "$BINDIR/hal-command"
echo "Installed binary to $BINDIR/hal-command"

# Install config
CONFDIR="${HOME}/.config/hal-command"
mkdir -p "$CONFDIR"
if [ ! -f "$CONFDIR/config.toml" ]; then
    cp config/config.toml "$CONFDIR/config.toml"
    echo "Installed default config to $CONFDIR/config.toml"
    echo "  Edit the server URL in this file!"
else
    echo "Config already exists at $CONFDIR/config.toml (not overwritten)"
fi

if [ ! -f "$CONFDIR/style.css" ]; then
    cp config/style.css "$CONFDIR/style.css"
    echo "Installed default stylesheet to $CONFDIR/style.css"
else
    echo "Stylesheet already exists at $CONFDIR/style.css (not overwritten)"
fi

echo ""
echo "=== Add to Hyprland ==="
echo "Add this line to ~/.config/hypr/hyprland.conf:"
echo ""
echo "  bind = SUPER, H, exec, hal-command"
echo ""
echo "Done!"
