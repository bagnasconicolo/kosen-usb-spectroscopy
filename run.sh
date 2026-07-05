#!/usr/bin/env bash
#
# KOSEN USB Spectroscopy - Linux launcher
# Creates a virtual environment (if missing), installs dependencies,
# activates it and starts the application.
#
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$PROJECT_DIR"

# On many Linux distros PyQt6 needs a few system libraries for the "xcb"
# platform plugin. If the app fails with a Qt platform error, install:
#   Debian/Ubuntu: sudo apt install libxcb-cursor0 libxcb-xinerama0 libgl1
#   Fedora:        sudo dnf install xcb-util-cursor mesa-libGL
#   Arch:          sudo pacman -S xcb-util-cursor libglvnd

venv_is_valid() {
    [ -f "$VENV_DIR/bin/activate" ] && [ -x "$VENV_DIR/bin/python" ]
}

create_venv() {
    echo "📦 Creating virtual environment..."
    # A previous failed attempt can leave a broken venv/ dir behind.
    rm -rf "$VENV_DIR"

    if ! "$PYTHON_BIN" -m venv "$VENV_DIR" 2>/tmp/kosen_venv_err; then
        echo ""
        echo "❌ Could not create the virtual environment."
        cat /tmp/kosen_venv_err
        echo ""
        echo "On Debian/Ubuntu install the venv module first:"
        echo "    sudo apt update && sudo apt install -y python3-venv python3-pip"
        echo "On Fedora:   sudo dnf install -y python3-virtualenv python3-pip"
        echo "On Arch:     sudo pacman -S --needed python-pip"
        exit 1
    fi

    if ! venv_is_valid; then
        echo ""
        echo "❌ The virtual environment was created but is incomplete"
        echo "   (missing bin/activate). This usually means 'python3-venv'"
        echo "   is not installed. On Debian/Ubuntu run:"
        echo "       sudo apt install -y python3-venv python3-pip"
        exit 1
    fi

    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    echo "⬆️  Installing dependencies..."
    pip install --upgrade pip setuptools wheel
    pip install -r "$PROJECT_DIR/requirements.txt"
}

if venv_is_valid; then
    echo "🔄 Activating existing virtual environment..."
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
else
    # Missing OR broken from a previous failed run -> (re)create it.
    create_venv
fi

print_qt_platform_help() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        id="$ID"; like="$ID_LIKE"
    fi

    echo ""
    echo "❌ Qt could not initialize a display platform plugin (usually 'xcb')."
    echo "   PyQt6 needs some SYSTEM libraries that pip cannot install."
    echo ""
    case " $id $like " in
        *debian*|*ubuntu*|*mint*|*pop*)
            echo "   Install them with:"
            echo "     sudo apt update && sudo apt install -y \\"
            echo "       libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \\"
            echo "       libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \\"
            echo "       libxkbcommon-x11-0 libgl1"
            ;;
        *fedora*|*rhel*|*centos*|*rocky*|*alma*)
            echo "   Install them with:"
            echo "     sudo dnf install -y xcb-util-cursor xcb-util-wm xcb-util-image \\"
            echo "       xcb-util-keysyms xcb-util-renderutil libxkbcommon-x11 mesa-libGL"
            ;;
        *arch*|*manjaro*|*endeavour*)
            echo "   Install them with:"
            echo "     sudo pacman -S --needed xcb-util-cursor xcb-util-wm xcb-util-image \\"
            echo "       xcb-util-keysyms xcb-util-renderutil libxkbcommon-x11 libglvnd"
            ;;
        *suse*|*opensuse*)
            echo "   Install them with:"
            echo "     sudo zypper install -y xcb-util-cursor libxkbcommon-x11-0 Mesa-libGL1"
            ;;
        *)
            echo "   Install the xcb-util-cursor / libxcb-cursor, xcb-util-*, "
            echo "   libxkbcommon-x11 and libGL packages for your distribution."
            ;;
    esac

    if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
        echo ""
        echo "   You are on a Wayland session. If it still fails after installing"
        echo "   the libraries above, try forcing X11:"
        echo "     QT_QPA_PLATFORM=xcb ./run.sh"
    fi

    echo ""
    echo "   To see exactly which library is missing, run:"
    echo "     QT_DEBUG_PLUGINS=1 python3 kosen_spectroscopy.py 2>&1 | grep -i xcb"
    echo ""
}

echo "🚀 Starting KOSEN USB Spectroscopy..."
STDERR_LOG="$(mktemp)"
set +e
# Capture stderr to a file (race-free), then echo it back to the terminal.
python3 "$PROJECT_DIR/kosen_spectroscopy.py" 2> "$STDERR_LOG"
status=$?
set -e
cat "$STDERR_LOG" >&2

if [ "$status" -ne 0 ] && grep -qiE "platform plugin|xcb|could be initialized" "$STDERR_LOG"; then
    print_qt_platform_help
fi
rm -f "$STDERR_LOG"
exit "$status"
