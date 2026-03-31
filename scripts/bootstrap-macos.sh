#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (c) 2026 Ivar Hogstad
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# See LICENSE file in the project root or <https://www.gnu.org/licenses/>.

# =============================================================================
# scripts/bootstrap-macos.sh — One-time macOS bootstrap script
# =============================================================================
#
# Run this script manually on a fresh macOS host BEFORE running the Ansible
# playbook for the first time. It installs Homebrew, which in turn installs
# Xcode Command Line Tools automatically (including Python 3).
#
# This only needs to be run once per machine. After this, the Ansible playbook
# handles everything else.
#
# Usage (run on the Mac itself, or via SSH):
#   bash <(curl -fsSL https://raw.githubusercontent.com/ivahos/ipv6_sync/main/scripts/bootstrap-macos.sh)
#
# Or if you have the repo cloned:
#   bash scripts/bootstrap-macos.sh

set -e

echo "==> Checking if Homebrew is already installed..."
if command -v brew &>/dev/null; then
    echo "==> Homebrew already installed at $(command -v brew), skipping."
else
    echo "==> Installing Homebrew (this will also install Xcode Command Line Tools)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

    # Add Homebrew to PATH for Apple Silicon Macs
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi

echo "==> Installing Python 3 via Homebrew..."
brew install python3

echo ""
echo "==> Bootstrap complete!"
echo "==> You can now run the Ansible playbook from your deploy machine:"
echo "    ansible-playbook ansible/site.yml --limit $(hostname)"
