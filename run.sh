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

echo "🚀 Starting KOSEN USB Spectroscopy..."
exec python3 "$PROJECT_DIR/kosen_spectroscopy.py"
