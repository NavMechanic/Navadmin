#!/bin/bash

# Project Athena - Standalone Launcher
# Run with: bash run_athena.sh

set -e

echo "==========================================="
echo "🚀 PROJECT ATHENA - STANDALONE LAUNCHER"
echo "==========================================="
echo ""

# Check Python
echo "✓ Checking Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.11+"
    exit 1
fi

python_version=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "  Using Python $python_version"
echo ""

# Install requirements
echo "✓ Installing dependencies..."
pip install -q -r requirements-standalone.txt 2>/dev/null || {
    echo "  Note: You can also run: pip install Flask PyJWT"
}
echo ""

# Run application
echo "✓ Starting Project Athena..."
echo ""
python3 athena_standalone.py
