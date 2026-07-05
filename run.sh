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
# platform plugin. If the app fails to start with a Qt platform error, install:
#   Debian/Ubuntu: sudo apt install libxcb-cursor0 libxcb-xinerama0 libgl1
#   Fedora:        sudo dnf install xcb-util-cursor mesa-libGL
#   Arch:          sudo pacman -S xcb-util-cursor libglvnd

if [ ! -d "$VENV_DIR" ]; then
    echo "📦 Creating virtual environment..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"

    echo "🔄 Activating and installing dependencies..."
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip setuptools wheel
    pip install -r "$PROJECT_DIR/requirements.txt"
else
    echo "🔄 Activating existing virtual environment..."
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
fi

echo "🚀 Starting KOSEN USB Spectroscopy..."
exec python3 "$PROJECT_DIR/kosen_spectroscopy.py"
