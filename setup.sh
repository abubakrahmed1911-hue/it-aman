#!/bin/bash
# ===============================================================
#  Script: setup.sh (Final )
# ===============================================================

if [[ $EUID -ne 0 ]]; then
   echo "Please run with sudo"
   exit 1
fi


FIXED_ADMIN="admin"

echo "--- Checking Admin User Status ---"


if id "$FIXED_ADMIN" &>/dev/null; then
    echo "User '$FIXED_ADMIN' already exists. Skipping password and creation."
else

    echo "------------------------------------------------"
    read -s -p "Enter NEW local password for $FIXED_ADMIN: " ADMIN_PASS
    echo ""
    
    echo "Creating local admin user: $FIXED_ADMIN..."
    useradd -m -s /bin/bash "$FIXED_ADMIN"
    

    pwconv
    

    echo "$FIXED_ADMIN:$ADMIN_PASS" | chpasswd --force-shadow 2>/dev/null
    
    if [ $? -eq 0 ]; then
        echo "Password for '$FIXED_ADMIN' set successfully."
    else
        echo "Standard bypass failed, using direct shadow update..."
        echo "$FIXED_ADMIN:$ADMIN_PASS" | chpasswd -c SHA512
    fi
fi


usermod -aG sudo "$FIXED_ADMIN" 2>/dev/null || usermod -aG wheel "$FIXED_ADMIN"
echo "Permissions verified for $FIXED_ADMIN."

echo "------------------------------------------------"


REAL_LOGIN_USER=$(logname 2>/dev/null || echo $SUDO_USER)
USER_HOME=$(getent passwd "$REAL_LOGIN_USER" | cut -d: -f6)

if [ -d "$USER_HOME/Desktop" ]; then
    DESKTOP_PATH="$USER_HOME/Desktop"
elif [ -d "$USER_HOME/سطح المكتب" ]; then
    DESKTOP_PATH="$USER_HOME/سطح المكتب"
else
    DESKTOP_PATH="$USER_HOME/Desktop"
    mkdir -p "$DESKTOP_PATH"
fi

echo "Installing IT Aman Tool for user: $REAL_LOGIN_USER"


if [ -f "printers.sh" ]; then
    cp printers.sh /usr/local/bin/it-aman
    chmod +x /usr/local/bin/it-aman
    chown root:root /usr/local/bin/it-aman
else
    echo "Error: printers.sh not found!"
    exit 1
fi


echo "$REAL_LOGIN_USER ALL=(ALL) NOPASSWD: /usr/local/bin/it-aman" > /etc/sudoers.d/it-aman-tool
chmod 0440 /etc/sudoers.d/it-aman-tool


cat <<EOF > "$DESKTOP_PATH/IT-Aman.desktop"
[Desktop Entry]
Version=1.0
Type=Application
Name=IT Aman Tool
Comment=Printer Repair Tool
Exec=sudo /usr/local/bin/it-aman
Icon=printer-error
Terminal=false
StartupNotify=true
StartupWMClass=zenity
Categories=System;Utility;
EOF

chown "$REAL_LOGIN_USER" "$DESKTOP_PATH/IT-Aman.desktop"
chmod +x "$DESKTOP_PATH/IT-Aman.desktop"
chattr +i "$DESKTOP_PATH/IT-Aman.desktop"
echo "--------------------------------------------------------"

echo "Locking printer settings..."

# Remove user from lpadmin
gpasswd -d "$REAL_LOGIN_USER" lpadmin 2>/dev/null

# Restrict CUPS admin access
sed -i 's/Require user @SYSTEM/Require user admin/' /etc/cups/cupsd.conf

# Restart CUPS
systemctl restart cups

echo "Printer settings locked."

echo "--------------------------------------------------------"
echo "Installation Complete and locked Icon Successfully ^_^ !"
echo "--------------------------------------------------------"
