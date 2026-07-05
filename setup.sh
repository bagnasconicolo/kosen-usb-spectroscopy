#!/bin/bash
#
# KOSEN USB Spectroscopy - macOS setup
# Creates a virtual environment and installs dependencies.
#
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"

echo "📦 Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"

echo "🔄 Activating virtual environment..."
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "⬆️  Upgrading pip, setuptools, wheel..."
pip install --upgrade pip setuptools wheel

echo "📥 Installing dependencies..."
pip install -r "$PROJECT_DIR/requirements.txt"

echo ""
echo "✅ Virtual environment ready!"
echo ""
echo "🎬 To start KOSEN USB Spectroscopy, run:"
echo "   source $VENV_DIR/bin/activate"
echo "   python3 kosen_spectroscopy.py"
echo ""
