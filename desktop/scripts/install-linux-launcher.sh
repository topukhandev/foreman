#!/bin/sh
# Registers the development app with the Linux desktop (GNOME dash, KDE, …): an application entry and its icon, so
# the window gets Foreman's icon and name in the dock and the launcher. A packaged build (`npm run tauri build`)
# installs its own entry; this is for `npm run tauri dev`.
#
# The window's app id is the identifier in src-tauri/tauri.conf.json (enableGTKAppId), and the desktop entry has the
# same name, which is how Wayland matches the two; StartupWMClass is the window class X11 sessions match (the program's
# name). Run again after moving the checkout.
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
id=dev.foreman.desktop
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
icons="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

for pair in 32x32:32x32.png 128x128:128x128.png 256x256:128x128@2x.png 512x512:icon.png; do
  size=${pair%%:*}; file=${pair#*:}
  mkdir -p "$icons/$size/apps"
  cp "$here/src-tauri/icons/$file" "$icons/$size/apps/$id.png"
done

# A desktop session doesn't have the shell's PATH (node from nvm, cargo from rustup): the launcher records the one
# used now. (An Exec line can't hold this itself: the desktop entry format restricts quoting.)
bin="${XDG_DATA_HOME:-$HOME/.local/share}/foreman-desktop"
mkdir -p "$bin" "$apps"
cat > "$bin/launch.sh" <<LAUNCH
#!/bin/sh
cd "$here" && PATH="$PATH" exec npm run tauri dev
LAUNCH
chmod +x "$bin/launch.sh"
cat > "$apps/$id.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Foreman
GenericName=AI work coordinator
Comment=Hand work to AI coding sessions across your projects
Exec="$bin/launch.sh"
Icon=$id
Terminal=false
Categories=Development;
StartupWMClass=foreman-desktop
StartupNotify=true
DESKTOP

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f "${icons%/hicolor}/hicolor" || true
echo "Installed $apps/$id.desktop and its icons."
echo "Start Foreman from the launcher (or the app grid); restart the app if it was already running."
