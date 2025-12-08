#!/bin/bash

# Must run as sudo
if [[ $EUID -ne 0 ]]; then
   echo "Run this script as root (sudo bash enable-root-login.sh)"
   exit 1
fi

echo "Setting root password..."
passwd root

echo "Updating SSH configuration..."
SSHD_CONFIG="/etc/ssh/sshd_config"

# Enable PermitRootLogin
sed -i 's/#\?PermitRootLogin.*/PermitRootLogin yes/' "$SSHD_CONFIG"

# Ensure PasswordAuthentication is enabled
sed -i 's/#\?PasswordAuthentication.*/PasswordAuthentication yes/' "$SSHD_CONFIG"

echo "Restarting SSH..."
systemctl restart ssh

echo "Root login enabled. Make sure you KNOW what you're doing!"

