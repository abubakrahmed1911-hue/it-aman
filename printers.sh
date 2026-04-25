#!/bin/bash
# ===============================================================
#  Script: it_aman_printer_fix.sh
#  Created by: Mahmoud Rabia Kassem (Specialist IT Admin)
#  Version: 1.3 - Final Stable For FS
# ===============================================================
CURRENT_VERSION="1.3"
# SECURITY FIX v1.3.1: GitHub PAT is never hard-coded in the script.
# Set the ITAMAN_GITHUB_TOKEN environment variable before running, e.g.:
#   export ITAMAN_GITHUB_TOKEN="ghp_..."
# If unset, updates use unauthenticated GitHub API (60 req/hour rate limit).
TOKEN="${ITAMAN_GITHUB_TOKEN:-}"
USER="mahmoudkassem30"
REPO="Printers-Tools"
BRANCH="main"

VERSION_URL="https://api.github.com/repos/$USER/$REPO/contents/version.txt?ref=$BRANCH"
SCRIPT_URL="https://api.github.com/repos/$USER/$REPO/contents/printers.sh?ref=$BRANCH"

check_for_updates() {
    # Network connectivity check first
    if ! curl -sf --connect-timeout 3 https://github.com -o /dev/null; then return; fi

    # Build auth header only if TOKEN is set
    local AUTH_HEADER=""
    [ -n "$TOKEN" ] && AUTH_HEADER="-H "Authorization: token $TOKEN""

    # RETRY FIX v1.3.1: retry up to 3 times with exponential backoff
    local REMOTE_VERSION=""
    for _RETRY in 1 2 3; do
        REMOTE_VERSION=$(curl -f -sL ${AUTH_HEADER:+$AUTH_HEADER} \
             -H "Accept: application/vnd.github.v3.raw" \
             --connect-timeout 5 "$VERSION_URL" 2>/dev/null | tr -d '[:space:]')
        [ -n "$REMOTE_VERSION" ] && break
        sleep $(( _RETRY * 2 ))
    done

    if [ -z "$REMOTE_VERSION" ]; then
        return
    fi

    if [ "$REMOTE_VERSION" != "$CURRENT_VERSION" ] && \
       [ "$(printf '%s\n%s\n' "$CURRENT_VERSION" "$REMOTE_VERSION" | sort -V | tail -1)" = "$REMOTE_VERSION" ]; then
        refresh_sys_icon
        read WIN_W WIN_H < <(get_win_size medium)
        zenity --question --title "تحديث متوفر New Update" \
               --text "يوجد إصدار جديد ($REMOTE_VERSION). هل تريد التحديث الآن؟" \
               --width=$WIN_W --window-icon="$SYS_ICON" 2>/dev/null

        if [ $? -eq 0 ]; then
            local TMP_SCRIPT="/tmp/printers_new_$$.sh"
            local TMP_HASH="/tmp/printers_new_$$.sha256"
            local BACKUP_PATH="/usr/local/bin/it-aman.backup.$(date +%Y%m%d%H%M%S)"

            # Download new script
            if ! curl -f -sL ${AUTH_HEADER:+$AUTH_HEADER} \
                -H "Accept: application/vnd.github.v3.raw" \
                "$SCRIPT_URL" -o "$TMP_SCRIPT" 2>/dev/null; then
                zenity --error --text "فشل تحميل ملف التحديث. تأكد من الاتصال بالانترنت." 2>/dev/null
                rm -f "$TMP_SCRIPT"
                return
            fi

            # HASH VERIFICATION FIX v1.3.1: download and verify SHA256 checksum
            local HASH_URL="https://api.github.com/repos/$USER/$REPO/contents/printers.sh.sha256?ref=$BRANCH"
            local REMOTE_HASH=""
            REMOTE_HASH=$(curl -f -sL ${AUTH_HEADER:+$AUTH_HEADER} \
                -H "Accept: application/vnd.github.v3.raw" \
                --connect-timeout 5 "$HASH_URL" 2>/dev/null | awk '{print $1}')

            if [ -n "$REMOTE_HASH" ]; then
                local LOCAL_HASH
                LOCAL_HASH=$(sha256sum "$TMP_SCRIPT" | awk '{print $1}')
                if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
                    zenity --error --title "خطأ أمني" \
                        --text "فشل التحقق من سلامة ملف التحديث (SHA256 mismatch).\nتم إلغاء التحديث." 2>/dev/null
                    rm -f "$TMP_SCRIPT"
                    return
                fi
            fi

            # ROLLBACK FIX v1.3.1: backup current version before replacing
            if [ -f /usr/local/bin/it-aman ]; then
                cp -f /usr/local/bin/it-aman "$BACKUP_PATH" 2>/dev/null
            fi

            # Atomic replacement
            chmod +x "$TMP_SCRIPT"
            if mv -f "$TMP_SCRIPT" /usr/local/bin/it-aman; then
                chown root:root /usr/local/bin/it-aman
                rm -f "$TMP_HASH"
                zenity --info --title "Done Updated /نجاح" \
                    --text "تم التحديث إلى $REMOTE_VERSION بنجاح.\nيرجى إعادة تشغيل الأداة." 2>/dev/null
                exit 0
            else
                # Restore backup on failure
                [ -f "$BACKUP_PATH" ] && mv -f "$BACKUP_PATH" /usr/local/bin/it-aman
                zenity --error --text "فشل تطبيق التحديث. تم استعادة النسخة السابقة." 2>/dev/null
                rm -f "$TMP_SCRIPT"
            fi
        fi
    fi
}
handle_error() {
    local error_point="$1"
    local REAL_USER=${SUDO_USER:-$USER}
    local USER_DESKTOP="/home/$REAL_USER/Desktop"
    local LOG_FILE="$USER_DESKTOP/it_aman_error.log"
    
    echo "--- Error Report ---" >> "$LOG_FILE"
    echo "Date: $(date)" >> "$LOG_FILE"
    echo "Failed at: $error_point" >> "$LOG_FILE"
    echo "--------------------" >> "$LOG_FILE"
    
    chown $REAL_USER:$REAL_USER "$LOG_FILE"
    
    zenity --error --title "Error" --text "حدث خطأ في: $error_point\nسيتم فتح سجل الأخطاء الآن." --width=300 2>/dev/null
    sudo -u "$REAL_USER" xdg-open "$LOG_FILE" &>/dev/null
}

if [ "$EUID" -ne 0 ]; then
    zenity --error --title "Error" --text "Administrator rights required. Please use sudo." 2>/dev/null
    exit 1
fi

REAL_USER=${SUDO_USER:-$USER}


CUPS_ADMIN_USER="admin"
export CUPS_SERVER=localhost
export IPP_PORT=631


CUPS_SUDOERS_FILE="/etc/sudoers.d/it-aman-cups"
if [ ! -f "$CUPS_SUDOERS_FILE" ]; then
    cat > "$CUPS_SUDOERS_FILE" <<'EOF'
admin ALL=(ALL) NOPASSWD: /usr/sbin/lpadmin, /usr/sbin/cupsenable, /usr/sbin/cupsaccept, /usr/sbin/cancel
EOF
    chmod 0440 "$CUPS_SUDOERS_FILE"
fi
TOOL_NAME="IT Aman - Printer Tool For FS v 1.3"
SYS_ICON_NAME="it-aman-printer"
SYS_ICON_PATH=""
SYS_ICON_URL="https://www.dropbox.com/scl/fi/94xo3vi1yo887xzkuu1mo/icon-printer.png?rlkey=z17g5rz3prmekiyvk78eom2ft&st=ock3tdn1&dl=1"
SYS_ICON_FILE="/usr/local/share/it-aman/icons/icon-printer.png"
SYS_ICON_THEME_FILE="/usr/share/icons/hicolor/128x128/apps/${SYS_ICON_NAME}.png"
DESKTOP_FILE="/usr/share/applications/it-aman-printer.desktop"
SCRIPT_ABS_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
for _ICON_CAND in \
    /usr/share/icons/hicolor/48x48/devices/printer.png \
    /usr/share/icons/Adwaita/48x48/devices/printer.png \
    /usr/share/icons/Adwaita/64x64/devices/printer.png
do
    if [ -f "$_ICON_CAND" ]; then
        SYS_ICON_PATH="$_ICON_CAND"
        break
    fi
done
SYS_ICON="${SYS_ICON_NAME}"

refresh_sys_icon() {
    if [ -s "$SYS_ICON_FILE" ]; then
        mkdir -p "$(dirname "$SYS_ICON_THEME_FILE")" >/dev/null 2>&1
        if [ ! -s "$SYS_ICON_THEME_FILE" ] || ! cmp -s "$SYS_ICON_FILE" "$SYS_ICON_THEME_FILE"; then
            cp -f "$SYS_ICON_FILE" "$SYS_ICON_THEME_FILE" >/dev/null 2>&1
            mkdir -p /usr/share/icons/hicolor/64x64/apps >/dev/null 2>&1
            cp -f "$SYS_ICON_FILE" /usr/share/icons/hicolor/64x64/apps/${SYS_ICON_NAME}.png >/dev/null 2>&1
            command -v gtk-update-icon-cache >/dev/null 2>&1 && \
                gtk-update-icon-cache -f -t /usr/share/icons/hicolor >/dev/null 2>&1
        fi
        SYS_ICON="$SYS_ICON_NAME"
    else
        SYS_ICON="${SYS_ICON_PATH:-printer}"
    fi
}

ensure_desktop_entry() {
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=IT Aman Printer Tool
Exec=${SCRIPT_ABS_PATH}
Icon=${SYS_ICON_FILE}
Terminal=false
Categories=Utility;System;
StartupWMClass=zenity
EOF
    command -v update-desktop-database >/dev/null 2>&1 && \
        update-desktop-database /usr/share/applications >/dev/null 2>&1
}

(
    if [ ! -s "$SYS_ICON_FILE" ]; then
        mkdir -p "$(dirname "$SYS_ICON_FILE")" >/dev/null 2>&1
        curl -fL --connect-timeout 8 --max-time 30 --retry 1 \
            -o "${SYS_ICON_FILE}.tmp" "$SYS_ICON_URL" >/dev/null 2>&1 \
            && mv -f "${SYS_ICON_FILE}.tmp" "$SYS_ICON_FILE"
    fi
) &
refresh_sys_icon
ensure_desktop_entry

# ── Dynamic Window Sizing ──
get_win_size() {
    local TYPE="${1:-medium}"
    local SCR_W SCR_H


    if command -v xrandr &>/dev/null; then
        read SCR_W SCR_H < <(xrandr 2>/dev/null \
            | grep -oE '[0-9]+x[0-9]+' | head -1 \
            | awk -Fx '{print $1, $2}')
    fi

    # fallback لو xrandr مش شغال
    [ -z "$SCR_W" ] && SCR_W=1920
    [ -z "$SCR_H" ] && SCR_H=1080

    local W H
    case "$TYPE" in
        small)
            W=$(( SCR_W * 28 / 100 )); H=$(( SCR_H * 28 / 100 ))
            [ "$W" -lt 320 ] && W=320;  [ "$W" -gt 480 ] && W=480
            [ "$H" -lt 180 ] && H=180;  [ "$H" -gt 300 ] && H=300
            ;;
        medium)
            W=$(( SCR_W * 35 / 100 )); H=$(( SCR_H * 38 / 100 ))
            [ "$W" -lt 420 ] && W=420;  [ "$W" -gt 600 ] && W=600
            [ "$H" -lt 260 ] && H=260;  [ "$H" -gt 420 ] && H=420
            ;;
        large)
            W=$(( SCR_W * 42 / 100 )); H=$(( SCR_H * 52 / 100 ))
            [ "$W" -lt 520 ] && W=520;  [ "$W" -gt 680 ] && W=680
            [ "$H" -lt 360 ] && H=360;  [ "$H" -gt 620 ] && H=620
            ;;
        wide)
            W=$(( SCR_W * 45 / 100 )); H=$(( SCR_H * 38 / 100 ))
            [ "$W" -lt 540 ] && W=540;  [ "$W" -gt 720 ] && W=720
            [ "$H" -lt 260 ] && H=260;  [ "$H" -gt 420 ] && H=420
            ;;
        progress)
            W=$(( SCR_W * 32 / 100 )); H=$(( SCR_H * 18 / 100 ))
            [ "$W" -lt 380 ] && W=380;  [ "$W" -gt 560 ] && W=560
            [ "$H" -lt 130 ] && H=130;  [ "$H" -gt 200 ] && H=200
            ;;
    esac
    echo "$W $H"
}

pick_thermal_size_value() {
    local OPT_LINE="$1"
    local TOKENS

    TOKENS=$(echo "$OPT_LINE" | tr ' ' '\n' | sed 's/^\*//' | cut -d'/' -f1)

    echo "$TOKENS" | grep -Ei '^Custom\.80x297mm$|^80[^0-9]*x[^0-9]*297(mm)?$|^w80h297$' | head -1 && return
    echo "$TOKENS" | grep -Ei '^Custom\.72x297mm$|^72[^0-9]*x[^0-9]*297(mm)?$|^w72h297$' | head -1 && return
    echo "$TOKENS" | grep -Ei '80[^0-9]*x[^0-9]*297|297[^0-9]*x[^0-9]*80|72[^0-9]*x[^0-9]*297|297[^0-9]*x[^0-9]*72' | head -1
}

extract_cups_tokens() {
    local OPT_LINE="$1"
    echo "$OPT_LINE" \
        | sed 's/^[^:]*://g' \
        | tr ' ' '\n' \
        | sed 's/^\*//' \
        | cut -d'/' -f1 \
        | sed '/^$/d'
}

resolve_thermal_size_from_tokens() {
    local TOKENS="$1"
    local TOKENS_NO_CUSTOM
    local V

    TOKENS_NO_CUSTOM=$(echo "$TOKENS" | grep -E -i -v 'custom')

    for V in 80x297mm 80x297 80mmx297mm 80mmx297 w80h297 72x297mm 72x297 72mmx297mm 72mmx297 w72h297; do
        echo "$TOKENS_NO_CUSTOM" | grep -E -i "^${V}$" | head -1 && return
    done
    echo "$TOKENS_NO_CUSTOM" | grep -E -i '80[^0-9]*x[^0-9]*297|297[^0-9]*x[^0-9]*80' | head -1 && return
    echo "$TOKENS_NO_CUSTOM" | grep -E -i '72[^0-9]*x[^0-9]*297|297[^0-9]*x[^0-9]*72' | head -1
}

resolve_forced_custom_size_from_tokens() {
    local TOKENS="$1"
    local TOKENS_CUSTOM

    TOKENS_CUSTOM=$(echo "$TOKENS" | grep -E -i 'custom')

    echo "$TOKENS_CUSTOM" | grep -E -i '^Custom\.72x297(mm)?$|^Custom72x297(mm)?$' | head -1 && return
    echo "$TOKENS_CUSTOM" | grep -E -i '^Custom\.72\.0x297\.0(mm)?$|^Custom72\.0x297\.0(mm)?$' | head -1 && return
    echo "$TOKENS_CUSTOM" | grep -E -i '72[^0-9]*x[^0-9]*297|297[^0-9]*x[^0-9]*72' | head -1 && return
    echo "Custom.72x297mm"
}

set_thermal_defaults() {
    local PRN="$1"
    local OPTS CUT_LINE CUT_VAL

    OPTS=$(sudo -u admin /usr/bin/lpoptions -p "$PRN" -l 2>/dev/null)
    [ -z "$OPTS" ] && return

    CUT_LINE=$(echo "$OPTS" | grep -i '^CutType[/:]' | head -1)
    CUT_VAL=$(echo "$CUT_LINE" \
        | tr ' ' '\n' \
        | sed 's/^\*//' \
        | cut -d'/' -f1 \
        | grep -Ei '^FullCut$|^Full$|^Cut$' \
        | head -1)

    if [ -n "$CUT_VAL" ]; then
        sudo -u admin /usr/bin/lpoptions -p "$PRN" -o "CutType=$CUT_VAL" 2>/dev/null
        sudo -u admin /usr/sbin/lpadmin -p "$PRN" -o "CutType=$CUT_VAL" 2>/dev/null
    fi
}

check_for_updates


INFO_FILE=$(mktemp)
echo "------------------------------------------------" >> $INFO_FILE
echo "        IT Aman - Printer Support Tool For FS       " >> $INFO_FILE
echo "------------------------------------------------" >> $INFO_FILE
echo "Developed by: Mahmoud Rabia Kassem" >> $INFO_FILE
echo "Specialist IT Admin" >> $INFO_FILE
echo "" >> $INFO_FILE
echo "This tool helps in resolving common printing issues." >> $INFO_FILE
echo "© All Rights Reserved 2026" >> $INFO_FILE

refresh_sys_icon
read WIN_W WIN_H < <(get_win_size medium)
zenity --text-info --title "Welcome" --window-icon="$SYS_ICON" --filename="$INFO_FILE" --width=$WIN_W --height=$WIN_H --checkbox="Proceed / استمرار" 2>/dev/null
rm -f "$INFO_FILE"


refresh_sys_icon
read WIN_W WIN_H < <(get_win_size medium)
USER_LANG=$(zenity --list --title "$TOOL_NAME" --window-icon="$SYS_ICON" --text "Select Interface Language / اختر لغة الواجهة" \
--radiolist --column "Select" --column "ID" --column "Language" \
TRUE "1" "العربية" FALSE "2" "English" --width=$WIN_W --height=$WIN_H 2>/dev/null)

if [ -z "$USER_LANG" ]; then exit 0; fi

if [ "$USER_LANG" == "1" ]; then
    TXT_MENU="قائمة الخدمات المتاحة:"
    TXT_SELECT_VIDEO="اختار لينك اذا لم يعمل اللينك الاول اختار الثاني :"
    TXT_VIDEO_1="Google Drive"
    TXT_VIDEO_2="DropBox"
    TXT_O1=" معالجة حشر الورق (ارشادات)"
    TXT_O2=" فحص النظام الذكي (كشف وحل تلقائي)"
    TXT_O3=" إدارة الطابعات (إضافة / حذف)"
    TXT_O4=" إدارة الطابعة الحرارية (إضافة / حذف)"
    TXT_O5=" اعاده تعريف الطابعه كبيره/حراريه (إصلاح مباشر)"
    TXT_O6=" إصلاح أوامر الطباعة (تنظيف الذاكرة العامة)"
    TXT_O7=" عرض الحالة العامة للطابعات"
    TXT_O8=" خروج"
    TXT_WAIT="جاري المعالجة، يرجى الانتظار..."
    TXT_SUCCESS="تمت العملية بنجاح ✅"
    JAM_TITLE="خطوات إزالة الورق العالق"
    JAM_MSG="⚠️ يرجى اتباع التعليمات التالية بدقة:\n\n1. أطفئ الطابعة وافصل كابل الكهرباء فوراً.\n2. افتح الأبواب المخصصة للورق.\n3. اسحب الورق العالق 'بكلتا اليدين' ببطء شديد.\n4. لا تستخدم القوة المفرطة أو أدوات حادة.\n\nاضغط OK للانتقال إلى الفيديو التوضيحي."
    REP_HDR="[ تقرير فحص IT Aman ]"
    REP_C_FX="- تم إعادة تشغيل خدمة الطباعة (CUPS)."
    REP_J_FX="- تم تنظيف مهام الطباعة العالقة."
    REP_E_FX="- تم اكتشاف طابعات معطلة وإعادة تنشيطها."
    PRINTER_LIST_MSG="اختر الطابعة التي تريد تنشيطها ومسح أوامرها:"
    ENABLE_MSG="جاري التنشيط ومسح الذاكرة..."
else
    TXT_MENU="Select a task to perform:"
    TXT_O1=" Paper Jam Guide"
    TXT_O2=" Smart System Diagnostic (Auto Fix)"
    TXT_O3=" Printer Management (Add / Remove)"
    TXT_O4=" Thermal Printer Management (Add / Remove)"
    TXT_O5=" Repair Printer (Direct Enable & Clear)"
    TXT_O6=" Quick Fix Print Spooler (General)"
    TXT_O7=" View Printer Status"
    TXT_O8=" Exit"
    TXT_SELECT_VIDEO="Choose one link; if the first link doesn't work, choose the second.:"
    TXT_VIDEO_1="Google Drive"
    TXT_VIDEO_2="DropBox"
    TXT_WAIT="Processing, please wait..."
    TXT_SUCCESS="Task completed successfully ✅"
    JAM_TITLE="Paper Jam Removal Steps"
    JAM_MSG="⚠️ Important Instructions:\n\n1. Power off printer and unplug power cable.\n2. Open the designated paper access doors.\n3. Pull the stuck paper slowly using 'both hands'.\n4. Avoid excessive force or sharp tools.\n\nClick OK for the video guide."
    REP_HDR="[ IT Aman Diagnostic Report ]"
    REP_C_FX="- Print service (CUPS) was restarted."
    REP_J_FX="- Stuck jobs have been cleared."
    REP_E_FX="- Disabled printers were re-enabled."
    PRINTER_LIST_MSG="Select printer to enable and clear:"
    ENABLE_MSG="Enabling and clearing jobs..."
fi

while true; do
    refresh_sys_icon
    read WIN_W WIN_H < <(get_win_size large)
    CHOICE=$(zenity --list --title "$TOOL_NAME" --window-icon="$SYS_ICON" --text "$TXT_MENU" \
    --radiolist --column "Select" --column "ID" --column "Option" \
    FALSE "1" "$TXT_O1" FALSE "2" "$TXT_O2" FALSE "3" "$TXT_O3" \
    FALSE "4" "$TXT_O4" \
    FALSE "5" "$TXT_O5" FALSE "6" "$TXT_O6" FALSE "7" "$TXT_O7" \
    FALSE "8" "$TXT_O8" \
    --width=$WIN_W --height=$WIN_H 2>/dev/null)

    if [ -z "$CHOICE" ] || [ "$CHOICE" == "8" ]; then exit 0; fi

    case "$CHOICE" in
        1)
            read WIN_W WIN_H < <(get_win_size medium)
            zenity --info --title "$JAM_TITLE" --window-icon="$SYS_ICON" --text "$JAM_MSG" --width=$WIN_W 2>/dev/null

            read WIN_W WIN_H < <(get_win_size medium)
            VIDEO_CHOICE=$(zenity --list --title "$JAM_TITLE" --window-icon="$SYS_ICON" \
                --text "$TXT_SELECT_VIDEO" \
                --column "ID" --column "Video Description" \
                "1" "$TXT_VIDEO_1" \
                "2" "$TXT_VIDEO_2" \
                --width=$WIN_W --height=$WIN_H 2>/dev/null)

            case "$VIDEO_CHOICE" in
                "1")
                    sudo -u "$REAL_USER" xdg-open "https://drive.google.com/file/d/1e3-7J6hr5yd3uyXSSu8rPJkFwMw8s-My/view?usp=sharing" &>/dev/null &
                    ;;
                "2")
                    sudo -u "$REAL_USER" xdg-open "https://www.dropbox.com/scl/fi/pg75dydlchtpju7j65kr2/Remove-paper-jam-inside-keyocera-UK-TECH-720p-h264.mp4?rlkey=obb9ghb14yq5l19dv4fdllwfd&st=mw2bixwi&dl=0" &>/dev/null &
                    ;;
            esac
            ;;

        2)
            DIAG_LOG=$(mktemp)
            (
            echo "10"; sleep 0.5
            if ! systemctl is-active --quiet cups; then
                systemctl restart cups; echo -e "$REP_C_FX" >> "$DIAG_LOG"
            fi
            echo "40"
            if [ -n "$(lpstat -o)" ]; then
                sudo -u admin /usr/sbin/cancel -a 2>/dev/null; echo -e "$REP_J_FX" >> "$DIAG_LOG"
            fi
            echo "70"
            DISABLED_PRINTERS=$(lpstat -p | grep "disabled" | awk '{print $2}')
            if [ -n "$DISABLED_PRINTERS" ]; then
                while read -r p; do
                    sudo -u admin /usr/sbin/cupsenable "$p"
                    sudo -u admin /usr/sbin/cupsaccept "$p"
                done <<< "$DISABLED_PRINTERS"
                echo -e "$REP_E_FX" >> "$DIAG_LOG"
            fi
            echo "100" ) | zenity --progress --title "$TOOL_NAME" --text "$TXT_WAIT" --auto-close 2>/dev/null

            if [ ! -s "$DIAG_LOG" ]; then
                FINAL_MSG="النظام يعمل بشكل جيد، لم يتم العثور على أخطاء برمجية."
            else
                FINAL_MSG=$(cat "$DIAG_LOG")
            fi
            read WIN_W WIN_H < <(get_win_size medium)
            zenity --info --title "تقرير الإصلاح" --text "<b>$REP_HDR</b>\n\n$FINAL_MSG\n\n$TXT_SUCCESS" --width=$WIN_W 2>/dev/null
            rm -f "$DIAG_LOG"
            ;;

          
        3)
            read WIN_W WIN_H < <(get_win_size medium)
            MGMT_CHOICE=$(zenity --list \
                --title "$TOOL_NAME" \
                --window-icon="$SYS_ICON" \
                --text "إدارة الطابعات / Printer Management:" \
                --radiolist --column "" --column "ID" --column "Action" \
                TRUE  "add"    "🖨️  إضافة طابعة جديدة / Add New Printer" \
                FALSE "remove" "🗑️  حذف طابعة / Remove Printer" \
                --width=$WIN_W --height=$WIN_H 2>/dev/null)

            [ -z "$MGMT_CHOICE" ] && continue

          
            if [ "$MGMT_CHOICE" == "add" ]; then

                SCAN_LOG="/tmp/ita_scan.log"
                FOUND_FILE="/tmp/ita_found.txt"
                PROG_FILE="/tmp/ita_scan_prog.txt"
                rm -f "$FOUND_FILE" "$SCAN_LOG" "$PROG_FILE"

                
                (
                    echo "5" > "$PROG_FILE"
                    echo "# 🔍 جاري اكتشاف الشبكة..." >> "$PROG_FILE"

                  
                    LOCAL_IP=$(ip route get 8.8.8.8 2>/dev/null | grep -oE 'src [0-9.]+' | awk '{print $2}')
                    SUBNET=$(echo "$LOCAL_IP" | cut -d. -f1-3)

                    echo "# 🌐 الشبكة / Network: $SUBNET.0/24" >> "$PROG_FILE"
                    echo "الشبكة: $SUBNET.0/24" >> "$SCAN_LOG"

                 
                    echo "10" > "$PROG_FILE"
                    echo "# 🔍 فحص CUPS backends..." >> "$PROG_FILE"

                    timeout 15 sudo -u admin lpinfo -v 2>/dev/null \
                        | awk '{print $2}' \
                        | grep -iE '^(ipp|ipps|lpd|socket)://' \
                        | grep -viE 'everywhere|driverless|localhost|127\.0\.0' \
                        > /tmp/ita_lpinfo_uris.txt

                    
                    echo "20" > "$PROG_FILE"
                    echo "# 🔍 فحص الشبكة على ports 631 و 9100..." >> "$PROG_FILE"

                    if [ -n "$SUBNET" ]; then
                        # scan سريع بـ bash على الـ subnet
                        for i in $(seq 1 254); do
                            HOST="$SUBNET.$i"
                            (
                               
                                if timeout 1 bash -c "echo >/dev/tcp/$HOST/631" 2>/dev/null || \
                                   timeout 1 bash -c "echo >/dev/tcp/$HOST/9100" 2>/dev/null; then
                                    echo "ipp://$HOST/ipp/print" >> /tmp/ita_scan_uris.txt
                                fi
                            ) &
                        done
                        wait
                    fi

                    echo "45" > "$PROG_FILE"
                    echo "# 🔍 جمع النتائج..." >> "$PROG_FILE"

                    
                    if command -v avahi-browse &>/dev/null; then
                        echo "# 🔍 mDNS scan..." >> "$PROG_FILE"
                        timeout 8 avahi-browse -t -r _ipp._tcp 2>/dev/null \
                            | grep -oE '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' \
                            | sort -u \
                            | while read -r IP; do
                                echo "ipp://$IP/ipp/print" >> /tmp/ita_scan_uris.txt
                            done
                    fi

                    cat /tmp/ita_lpinfo_uris.txt /tmp/ita_scan_uris.txt 2>/dev/null \
                        | sort -u \
                        | grep -viE 'localhost|127\.0\.0' \
                        > /tmp/ita_uris.txt
                    rm -f /tmp/ita_lpinfo_uris.txt /tmp/ita_scan_uris.txt

                    TOTAL=$(wc -l < /tmp/ita_uris.txt 2>/dev/null || echo 0)
                    echo "اكتشف $TOTAL طابعة محتملة" >> "$SCAN_LOG"

                    echo "50" > "$PROG_FILE"
                    echo "# 🖨️ جاري التعرف على الطابعات ($TOTAL)..." >> "$PROG_FILE"

                    COUNT=0
                    while read -r URI; do
                        [ -z "$URI" ] && continue
                        COUNT=$((COUNT + 1))

                        PRINTER_IP=$(echo "$URI" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}')
                        [ -z "$PRINTER_IP" ] && continue

                      
                        REAL_MODEL=""


                        WEB_PAGE=$(curl -sf --connect-timeout 3 \
                            -A "Mozilla/5.0" \
                            "http://$PRINTER_IP/" 2>/dev/null)

                        if [ -n "$WEB_PAGE" ]; then
                            REAL_MODEL=$(echo "$WEB_PAGE" \
                                | grep -ioE 'Model\s*:\s*[a-zA-Z0-9][a-zA-Z0-9\- ]{2,40}' \
                                | head -1 \
                                | sed 's/Model\s*:\s*//I' \
                                | xargs)

                            if [ -z "$REAL_MODEL" ]; then
                                REAL_MODEL=$(echo "$WEB_PAGE" \
                                    | grep -ioE '<title>[^<]{3,60}</title>' \
                                    | sed 's/<[^>]*>//g' \
                                    | grep -ioE '[a-zA-Z0-9][a-zA-Z0-9\- ]{4,40}' \
                                    | grep -iv 'home\|login\|welcome\|index\|web\|page' \
                                    | head -1 | xargs)
                            fi
                        fi


                        if [ -z "$REAL_MODEL" ]; then
                            for TRY_PATH in \
                                "/general/information.html" \
                                "/info/overview.html" \
                                "/web/guest/en/websys/webArch/mainFrame.cgi" \
                                "/cgi-bin/dynamic/config/status.html" \
                                "/status.cgi"
                            do
                                TRY_PAGE=$(curl -sf --connect-timeout 2 \
                                    -A "Mozilla/5.0" \
                                    "http://$PRINTER_IP${TRY_PATH}" 2>/dev/null)
                                if [ -n "$TRY_PAGE" ]; then
                                    REAL_MODEL=$(echo "$TRY_PAGE" \
                                        | grep -ioE 'Model\s*[:\-]?\s*[a-zA-Z0-9][a-zA-Z0-9\- ]{2,40}' \
                                        | head -1 \
                                        | sed 's/Model\s*[:\-]\s*//I' \
                                        | xargs)
                                    [ -n "$REAL_MODEL" ] && break
                                fi
                            done
                        fi


                        if [ -z "$REAL_MODEL" ] && command -v ipptool &>/dev/null; then
                            RAW_MODEL=$(timeout 5 ipptool -t "$URI" \
                                /usr/share/cups/ipptool/get-printer-attributes.test 2>/dev/null \
                                | grep -i 'printer-make-and-model' \
                                | grep -v 'REQUIRED\|STATUS\|EXPECT' \
                                | tail -1 \
                                | sed 's/.*= //;s/["\r]//g' \
                                | xargs)
                            REAL_MODEL=$(echo "$RAW_MODEL" \
                                | sed 's/KYOCERA Document Solutions Inc\.//gI' \
                                | sed 's/[Cc]orporation\b//g' \
                                | sed 's/\bInc\.\?//g' \
                                | xargs)
                        fi

                        # Fallback
                        [ -z "$REAL_MODEL" ] && REAL_MODEL="Printer @ $PRINTER_IP"


                        [ "$TOTAL" -gt 0 ] && PROG=$(( 50 + (COUNT * 45 / TOTAL) )) || PROG=80
                        echo "$PROG" > "$PROG_FILE"
                        echo "# 🖨️ ($COUNT/$TOTAL) $REAL_MODEL" >> "$PROG_FILE"
                        echo "$URI||$REAL_MODEL||$PRINTER_IP" >> "$FOUND_FILE"

                    done < /tmp/ita_uris.txt
                    rm -f /tmp/ita_uris.txt

                    echo "100" > "$PROG_FILE"
                    echo "# ✅ اكتمل البحث" >> "$PROG_FILE"
                    echo "DONE" >> "$PROG_FILE"

                ) &
                BG_PID=$!


                read _PW _PH < <(get_win_size progress)
                (
                    LAST_VAL=5
                    while kill -0 "$BG_PID" 2>/dev/null; do
                        if [ -f "$PROG_FILE" ]; then
                            NEW_VAL=$(grep '^[0-9]' "$PROG_FILE" | tail -1)
                            NEW_MSG=$(grep '^#' "$PROG_FILE" | tail -1)
                            if [ -n "$NEW_VAL" ] && [ "$NEW_VAL" != "$LAST_VAL" ]; then
                                echo "$NEW_VAL"
                                LAST_VAL="$NEW_VAL"
                            fi
                            [ -n "$NEW_MSG" ] && echo "$NEW_MSG"
                        fi
                        sleep 1
                    done
                    echo "100"
                    echo "# ✅ اكتمل / Done"
                ) | zenity --progress \
                    --title "$TOOL_NAME" \
                    --window-icon="$SYS_ICON" \
                    --text "🔍 جاري البحث عن الطابعات على الشبكة...\n🔍 Scanning network for printers..." \
                    --width=$_PW --height=$_PH \
                    --no-cancel \
                    2>/dev/null

                wait "$BG_PID"
                rm -f "$PROG_FILE"


                DISCOVERED_LIST=""
                [ -f "$FOUND_FILE" ] && DISCOVERED_LIST=$(cat "$FOUND_FILE")
                rm -f "$FOUND_FILE"

                if [ -z "$DISCOVERED_LIST" ]; then
                    read WIN_W WIN_H < <(get_win_size medium)
                    zenity --warning \
                        --title "$TOOL_NAME" \
                        --window-icon="$SYS_ICON" \
                        --text "⚠️ لم يتم العثور على طابعات شبكة.\n⚠️ No network printers found.\n\nتأكد من:\n• تشغيل الطابعة\n• الاتصال بنفس الشبكة\n• تفعيل خدمة CUPS\n\nLog: $SCAN_LOG" \
                        --width=$WIN_W 2>/dev/null
                    continue
                fi


                ZENITY_ARGS=()
                while IFS='||' read -r URI MODEL IP; do
                    [ -z "$URI" ] && continue
                    ZENITY_ARGS+=("$URI" "$MODEL" "$IP")
                done <<< "$DISCOVERED_LIST"

                SELECTED_URI=$(zenity --list \
                    --title "$TOOL_NAME" \
                    --window-icon="$SYS_ICON" \
                    --text "🖨️ اختر الطابعة لإضافتها:\n🖨️ Select printer to add:" \
                    --column "URI" --column "الموديل / Model" --column "IP Address" \
                    --hide-column=1 --print-column=1 \
                    "${ZENITY_ARGS[@]}" \
                    --width=$WIN_W --height=$WIN_H 2>/dev/null)

                [ -z "$SELECTED_URI" ] && continue


                SELECTED_LINE=$(echo "$DISCOVERED_LIST" | grep "^${SELECTED_URI}||")
                SELECTED_MODEL=$(echo "$SELECTED_LINE" | awk -F'\\|\\|' '{print $2}')
                SELECTED_IP=$(echo "$SELECTED_LINE" | awk -F'\\|\\|' '{print $3}')
                [ -z "$SELECTED_MODEL" ] && SELECTED_MODEL="Printer"


                PRINTER_NAME="printer-FS"



                LPD_URI="lpd://$SELECTED_IP/queue"


                PPD_MODEL=$(lpinfo -m 2>/dev/null \
                    | grep -i "ECOSYS M3550idn" \
                    | grep -i "KPDL" \
                    | head -1 | awk '{print $1}')

                # fallback: بحث بـ M3550idn فقط
                if [ -z "$PPD_MODEL" ]; then
                    PPD_MODEL=$(lpinfo -m 2>/dev/null \
                        | grep -i "M3550idn" \
                        | head -1 | awk '{print $1}')
                fi

                # fallback: بحث بـ ECOSYS
                if [ -z "$PPD_MODEL" ]; then
                    PPD_MODEL=$(lpinfo -m 2>/dev/null \
                        | grep -i "ECOSYS" \
                        | head -1 | awk '{print $1}')
                fi

                # Generic fallback
                [ -z "$PPD_MODEL" ] && PPD_MODEL="drv:///sample.drv/generic.ppd"


                if [ "$PPD_MODEL" == "drv:///sample.drv/generic.ppd" ]; then

                    KYO_DEB_URL="https://www.dropbox.com/scl/fi/u4ilpehz9aeemnnfeec6z/kyodialog_9.3-0_amd64.deb?rlkey=re8satdq4iduzxaqugb7l0oqw&st=4a85xjj9&dl=1"
                    KYO_DEB_FILE="/tmp/kyodialog_9.3-0_amd64.deb"
                    KYO_LOG="/tmp/ita_kyocera.log"
                    PROG_FILE="/tmp/ita_kyo_prog.txt"


                    echo "3"  > "$PROG_FILE"
                    echo "# 🔍 جاري التحقق من التعريفات..." >> "$PROG_FILE"


                    (
                        echo "$(date): بدء التحميل" > "$KYO_LOG"

                        echo "8"  > "$PROG_FILE"; echo "# 📥 جاري تحميل Kyocera Package..." >> "$PROG_FILE"


                        # dl=1 في الـ URL يجبر Dropbox على التحميل المباشر
                        curl -fL \
                            --connect-timeout 20 \
                            --max-time 120 \
                            --retry 2 \
                            -o "$KYO_DEB_FILE" \
                            "$KYO_DEB_URL" 2>>"$KYO_LOG"

                        if [ $? -ne 0 ] || [ ! -s "$KYO_DEB_FILE" ]; then
                            echo "$(date): فشل التحميل" >> "$KYO_LOG"
                            echo "FAIL" > "$PROG_FILE"
                        else
                            echo "$(date): اكتمل التحميل — $(du -sh "$KYO_DEB_FILE" | cut -f1)" >> "$KYO_LOG"
                            echo "55" > "$PROG_FILE"; echo "# ⚙️ جاري التثبيت... / Installing..." >> "$PROG_FILE"


                            DEBIAN_FRONTEND=noninteractive dpkg -i "$KYO_DEB_FILE" >>"$KYO_LOG" 2>&1


                            apt-get install -f -y >>"$KYO_LOG" 2>&1

                            echo "$(date): اكتمل dpkg" >> "$KYO_LOG"
                            rm -f "$KYO_DEB_FILE"

                            echo "80" > "$PROG_FILE"; echo "# 🔄 إعادة تشغيل CUPS..." >> "$PROG_FILE"
                            systemctl restart cups >>"$KYO_LOG" 2>&1
                            sleep 3

                            echo "100" > "$PROG_FILE"; echo "# ✅ اكتمل التثبيت" >> "$PROG_FILE"
                            echo "DONE" >> "$PROG_FILE"
                        fi

                    ) &
                    BG_PID=$!


                    read _PW _PH < <(get_win_size progress)
                    (
                        LAST_VAL=3
                        while kill -0 "$BG_PID" 2>/dev/null; do
                            if [ -f "$PROG_FILE" ]; then
                                NEW_VAL=$(grep '^[0-9]' "$PROG_FILE" | tail -1)
                                NEW_MSG=$(grep '^#' "$PROG_FILE" | tail -1)

                                if [ -n "$NEW_VAL" ] && [ "$NEW_VAL" != "$LAST_VAL" ]; then
                                    echo "$NEW_VAL"
                                    LAST_VAL="$NEW_VAL"
                                fi
                                [ -n "$NEW_MSG" ] && echo "$NEW_MSG"

                                grep -q "^FAIL" "$PROG_FILE" 2>/dev/null && break
                            fi
                            sleep 1
                        done
                        echo "100"
                        echo "# ✅ اكتمل / Done"
                    ) | zenity --progress \
                        --title "$TOOL_NAME" \
                        --window-icon="$SYS_ICON" \
                        --text "📦 Kyocera Printing Package\n\n📥 جاري التحميل والتثبيت تلقائياً...\n📥 Auto downloading and installing..." \
                        --width=$_PW --height=$_PH \
                        --no-cancel \
                        2>/dev/null

                    wait "$BG_PID"
                    rm -f "$PROG_FILE"


                    PPD_MODEL=$(lpinfo -m 2>/dev/null \
                        | grep -i "ECOSYS M3550idn" \
                        | grep -i "KPDL" \
                        | head -1 | awk '{print $1}')

                    [ -z "$PPD_MODEL" ] && PPD_MODEL=$(lpinfo -m 2>/dev/null \
                        | grep -i "M3550idn" \
                        | head -1 | awk '{print $1}')

                    [ -z "$PPD_MODEL" ] && PPD_MODEL=$(lpinfo -m 2>/dev/null \
                        | grep -i "ECOSYS" \
                        | head -1 | awk '{print $1}')

                    [ -z "$PPD_MODEL" ] && PPD_MODEL=$(lpinfo -m 2>/dev/null \
                        | grep -i "Kyocera" \
                        | grep -i "KPDL" \
                        | head -1 | awk '{print $1}')

                    if [ -z "$PPD_MODEL" ]; then
                        PPD_MODEL="drv:///sample.drv/generic.ppd"
                        zenity --warning \
                            --title "$TOOL_NAME" \
                            --window-icon="$SYS_ICON" \
                            --text "⚠️ لم يتم العثور على تعريف Kyocera بعد التثبيت.\n⚠️ Driver not found after install.\n\nسيتم استخدام Generic.\nUsing Generic as fallback.\n\nLog: $KYO_LOG" \
                            --width=$WIN_W 2>/dev/null
                    fi
                fi


                read _PW _PH < <(get_win_size progress)
                (
                    echo "10"

                    lpstat -e 2>/dev/null | grep -q "^$PRINTER_NAME$" \
                        && sudo -u admin /usr/sbin/lpadmin -x "$PRINTER_NAME" 2>/dev/null
                    echo "30"
                    sudo -u admin /usr/sbin/lpadmin \
                        -p "$PRINTER_NAME" -E \
                        -v "$LPD_URI" \
                        -m "$PPD_MODEL" \
                        -D "$SELECTED_MODEL" \
                        -L "Network - $SELECTED_IP" \
                        2>/tmp/ita_err
                    echo "75"
                    sudo -u admin /usr/sbin/cupsenable "$PRINTER_NAME" 2>/dev/null
                    sudo -u admin /usr/sbin/cupsaccept "$PRINTER_NAME" 2>/dev/null
                    echo "85"


                    PAPERFEED_KEY=$(sudo -u admin /usr/bin/lpoptions \
                        -p "$PRINTER_NAME" -l 2>/dev/null \
                        | grep -i 'paper.*feed\|feed.*paper\|InputSlot\|MediaSource' \
                        | head -1 \
                        | cut -d'/' -f1 | xargs)


                    if [ -z "$PAPERFEED_KEY" ]; then
                        for TRY_KEY in InputSlot MediaSource PaperFeed KCFeeder; do
                            CHECK=$(sudo -u admin /usr/bin/lpoptions \
                                -p "$PRINTER_NAME" -l 2>/dev/null \
                                | grep -i "^$TRY_KEY" | head -1)
                            if [ -n "$CHECK" ]; then
                                PAPERFEED_KEY="$TRY_KEY"
                                break
                            fi
                        done
                    fi


                    PAPERFEED_VAL=""
                    if [ -n "$PAPERFEED_KEY" ]; then
                        PAPERFEED_VAL=$(sudo -u admin /usr/bin/lpoptions \
                            -p "$PRINTER_NAME" -l 2>/dev/null \
                            | grep -i "^$PAPERFEED_KEY" \
                            | grep -ioE '\bOne\b|\bCassette1\b|\bTray1\b|\bUpper\b' \
                            | head -1)
                        [ -z "$PAPERFEED_VAL" ] && PAPERFEED_VAL="One"
                    fi


                    if [ -n "$PAPERFEED_KEY" ] && [ -n "$PAPERFEED_VAL" ]; then
                        sudo -u admin /usr/bin/lpoptions \
                            -p "$PRINTER_NAME" \
                            -o "${PAPERFEED_KEY}=${PAPERFEED_VAL}" \
                            -o Duplex=None \
                            2>/dev/null
                        sudo -u admin /usr/sbin/lpadmin \
                            -p "$PRINTER_NAME" \
                            -o "${PAPERFEED_KEY}=${PAPERFEED_VAL}" \
                            -o Duplex=None \
                            2>/dev/null
                    else
                        # Duplex فقط لو مش لاقي Paper Feed key
                        sudo -u admin /usr/bin/lpoptions \
                            -p "$PRINTER_NAME" \
                            -o Duplex=None \
                            2>/dev/null
                    fi
                    echo "100"
                ) | zenity --progress \
                    --title "$TOOL_NAME" \
                    --window-icon="$SYS_ICON" \
                    --text "⚙️ جاري تثبيت الطابعة...\n⚙️ Installing printer..." \
                    --auto-close --width=$_PW --height=$_PH 2>/dev/null

                read _IW _IH < <(get_win_size medium)
                if lpstat -e 2>/dev/null | grep -q "^$PRINTER_NAME$"; then
                    zenity --info \
                        --title "نجاح / Success" \
                        --window-icon="$SYS_ICON" \
                        --text "✅ تمت الإضافة بنجاح!\n✅ Printer added successfully!\n\n🖨️ الاسم / Name:\n    $PRINTER_NAME\n\n📡 الموديل / Model:\n    $SELECTED_MODEL\n\n🌐 IP: $SELECTED_IP\n\n🔌 Protocol: LPD\n\n🔧 Driver: $PPD_MODEL\n\n📄 Paper Feeder: One\n🔁 Duplex: Off" \
                        --width=$_IW 2>/dev/null
                else
                    ERR_MSG=$(cat /tmp/ita_err 2>/dev/null | head -5)
                    zenity --error \
                        --title "خطأ / Error" \
                        --window-icon="$SYS_ICON" \
                        --text "❌ فشلت إضافة الطابعة.\n❌ Failed to add printer.\n\n$ERR_MSG" \
                        --width=$_IW 2>/dev/null
                fi
                rm -f /tmp/ita_err


            elif [ "$MGMT_CHOICE" == "remove" ]; then

                ALL_PRINTERS=$(lpstat -e 2>/dev/null)
                if [ -z "$ALL_PRINTERS" ]; then
                    zenity --warning \
                        --title "$TOOL_NAME" \
                        --window-icon="$SYS_ICON" \
                        --text "⚠️ لا توجد طابعات مضافة في النظام.\n⚠️ No printers found in the system." \
                    --width=$WIN_W 2>/dev/null
                    continue
                fi

                ZENITY_ARGS=()
                while read -r PNAME; do
                    [ -z "$PNAME" ] && continue
                    URI=$(lpstat -v "$PNAME" 2>/dev/null | awk '{print $NF}')
                    if echo "$URI" | grep -qiE 'ipp|lpd|socket|http|smb'; then
                        PTYPE="🌐 شبكة / Network"
                    elif echo "$URI" | grep -qiE 'usb|direct|parallel'; then
                        PTYPE="🔌 USB / حرارية"
                    else
                        PTYPE="❓ أخرى"
                    fi
                    ZENITY_ARGS+=("$PNAME" "$PTYPE" "$URI")
                done <<< "$ALL_PRINTERS"

                SELECTED=$(zenity --list \
                    --title "$TOOL_NAME" \
                    --window-icon="$SYS_ICON" \
                    --text "🗑️ اختر الطابعة للحذف:\n🗑️ Select printer to remove:" \
                    --column "الاسم / Name" \
                    --column "النوع / Type" \
                    --column "العنوان / URI" \
                    --print-column=1 \
                    "${ZENITY_ARGS[@]}" \
                    --width=$WIN_W --height=$WIN_H 2>/dev/null)

                [ -z "$SELECTED" ] && continue

                zenity --question \
                    --title "تأكيد / Confirm" \
                    --window-icon="$SYS_ICON" \
                    --text "⚠️ هل أنت متأكد من حذف:\n⚠️ Confirm removal of:\n\n🖨️  $SELECTED" \
                    --ok-label="نعم، احذف / Yes, Remove" \
                    --cancel-label="إلغاء / Cancel" \
                    --width=$WIN_W 2>/dev/null

                [ $? -ne 0 ] && continue

                read _PW _PH < <(get_win_size progress)
                (
                    echo "20"
                    sudo -u admin /usr/sbin/cancel -a "$SELECTED" 2>/dev/null
                    cancel -a "$SELECTED" 2>/dev/null
                    echo "50"
                    sudo -u admin /usr/sbin/cupsdisable "$SELECTED" 2>/dev/null
                    sleep 1
                    echo "70"
                    sudo -u admin /usr/sbin/lpadmin -x "$SELECTED" 2>/tmp/ita_err
                    echo "100"
                ) | zenity --progress \
                    --title "$TOOL_NAME" \
                    --window-icon="$SYS_ICON" \
                    --text "🗑️ جاري حذف الطابعة...\nRemoving printer..." \
                    --auto-close --width=$_PW --height=$_PH 2>/dev/null

                read _IW _IH < <(get_win_size medium)
                if ! lpstat -e 2>/dev/null | grep -q "^$SELECTED$"; then
                    zenity --info \
                        --title "نجاح / Success" \
                        --window-icon="$SYS_ICON" \
                        --text "✅ تم الحذف بنجاح!\n✅ Printer removed successfully!\n\n🖨️  $SELECTED" \
                        --width=$_IW 2>/dev/null
                else
                    ERR_MSG=$(cat /tmp/ita_err 2>/dev/null | head -5)
                    zenity --error \
                        --title "خطأ / Error" \
                        --window-icon="$SYS_ICON" \
                        --text "❌ فشل الحذف.\n❌ Failed to remove printer.\n\n$ERR_MSG" \
                        --width=$_IW 2>/dev/null
                fi
                rm -f /tmp/ita_err
            fi
            ;;


        4)
            read WIN_W WIN_H < <(get_win_size medium)
            THERMAL_CHOICE=$(zenity --list \
                --title "$TOOL_NAME" \
                --window-icon="$SYS_ICON" \
                --text "إدارة الطابعة الحرارية / Thermal Printer Management:" \
                --radiolist --column "" --column "ID" --column "Action" \
                TRUE  "add"    "🖨️  إضافة طابعة حرارية / Add Thermal Printer" \
                FALSE "remove" "🗑️  حذف طابعة حرارية / Remove Thermal Printer" \
                --width=$WIN_W --height=$WIN_H 2>/dev/null)

            [ -z "$THERMAL_CHOICE" ] && continue

            if [ "$THERMAL_CHOICE" == "add" ]; then


                read WIN_W WIN_H < <(get_win_size medium)
                zenity --warning \
                    --title "⚠️ تنبيه مهم / Important Notice" \
                    --window-icon="$SYS_ICON" \
                    --text "⚠️ برجاء التأكد جيداً من نوع الطابعة قبل الاختيار\n⚠️ Please verify your printer model carefully before selecting\n\n🔍 انظر إلى الطابعة بشكل مباشر وتأكد من اسمها:\n    • هل هي SPRT (مكتوب عليها SPRT)؟\n    • أم هي X-Printer XP-80 (مكتوب عليها XP-80)؟\n\n🔍 Look at your printer physically and confirm the model:\n    • Is it SPRT (labeled SPRT on the device)?\n    • Or X-Printer XP-80 (labeled XP-80)?\n\n⚡ الاختيار الخاطئ قد يسبب مشكلة في التعريف\n⚡ Wrong selection may cause driver issues" \
                    --width=$WIN_W 2>/dev/null



                XP_IMG="/tmp/ita_xprinter.jpg"
                SPRIT_IMG="/tmp/ita_sprit.jpg"
                curl -sfL --connect-timeout 8 \
                    "https://www.dropbox.com/scl/fi/x3n5vi864jh3796amluhy/Xprinter-xp80.jpg?rlkey=g1dpnjlpgmmignft7s5klhkji&st=exg9p355&dl=1" \
                    -o "$XP_IMG" 2>/dev/null
                curl -sfL --connect-timeout 8 \
                    "https://www.dropbox.com/scl/fi/2ku07bgcvvep7xr21mhiq/SPRIT.jpg?rlkey=683g446bhtab2c8smhh0bfo71&st=b26f0r34&dl=1" \
                    -o "$SPRIT_IMG" 2>/dev/null


                THERMAL_BRAND=""
                RESULT_FILE="/tmp/ita_thermal_choice.txt"
                rm -f "$RESULT_FILE"

                DISPLAY_OK=0
                [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ] && DISPLAY_OK=1

                if [ "$DISPLAY_OK" -eq 1 ] && python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk" 2>/dev/null; then
                    sudo -u "$REAL_USER" python3 - "$XP_IMG" "$SPRIT_IMG" "$RESULT_FILE" "$SYS_ICON" << 'PYEOF'
import sys, os, gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GdkPixbuf

xp_img_path   = sys.argv[1]
sprit_img_path = sys.argv[2]
result_file    = sys.argv[3]
icon_arg       = sys.argv[4] if len(sys.argv) > 4 else ""

win = Gtk.Window()
win.set_title("اختر نوع الطابعة الحرارية / Select Thermal Printer")
if icon_arg:
    try:
        if os.path.isfile(icon_arg):
            win.set_icon_from_file(icon_arg)
        else:
            win.set_icon_name(icon_arg)
    except Exception:
        pass
win.set_border_width(20)
win.set_resizable(False)
win.connect("destroy", Gtk.main_quit)


css = b"""
window { background-color: #1e1e2e; }
label.title { color: #cba6f7; font-size: 16px; font-weight: bold; margin-bottom: 10px; }
button { background: #313244; border: 2px solid #45475a; border-radius: 12px; padding: 12px; }
button:hover { background: #3d3f55; border-color: #cba6f7; }
label.name { color: #cba6f7; font-size: 14px; font-weight: bold; margin-top: 8px; }
label.sub  { color: #a6adc8; font-size: 11px; }
"""
provider = Gtk.CssProvider()
provider.load_from_data(css)
Gtk.StyleContext.add_provider_for_screen(
    win.get_screen(), provider,
    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
)

main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
win.add(main_box)


title = Gtk.Label(label="اختر نوع الطابعة الحرارية\nSelect Thermal Printer Type")
title.get_style_context().add_class("title")
main_box.pack_start(title, False, False, 0)

cards_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
cards_box.set_halign(Gtk.Align.CENTER)
main_box.pack_start(cards_box, False, False, 0)

def make_card(img_path, name, subtitle, choice_val):
    btn = Gtk.Button()
    btn.get_style_context().add_class("flat")
    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    inner.set_halign(Gtk.Align.CENTER)

    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(img_path, 160, 130, True)
        img_widget = Gtk.Image.new_from_pixbuf(pixbuf)
    except Exception:
        img_widget = Gtk.Image.new_from_icon_name("printer", Gtk.IconSize.DIALOG)
    inner.pack_start(img_widget, False, False, 0)
    lbl_name = Gtk.Label(label=name)
    lbl_name.get_style_context().add_class("name")
    inner.pack_start(lbl_name, False, False, 0)
    lbl_sub = Gtk.Label(label=subtitle)
    lbl_sub.get_style_context().add_class("sub")
    inner.pack_start(lbl_sub, False, False, 0)
    btn.add(inner)
    def on_click(b, val=choice_val):
        with open(result_file, 'w') as f:
            f.write(val)
        Gtk.main_quit()
    btn.connect("clicked", on_click)
    return btn

cards_box.pack_start(make_card(xp_img_path,   "X-Printer", "XP-80 Series",    "xprinter"), False, False, 0)
cards_box.pack_start(make_card(sprit_img_path, "SPRT",      "80mm Thermal",    "sprt"),     False, False, 0)

win.show_all()
Gtk.main()
PYEOF
                    [ -f "$RESULT_FILE" ] && THERMAL_BRAND=$(cat "$RESULT_FILE")
                fi

                # Fallback: zenity عادي لو GTK مش متاح أو المستخدم أغلق النافذة
                if [ -z "$THERMAL_BRAND" ]; then
                    read WIN_W WIN_H < <(get_win_size medium)
                    THERMAL_BRAND=$(zenity --list \
                        --title "$TOOL_NAME" \
                        --window-icon="$SYS_ICON" \
                        --text "اختر نوع الطابعة الحرارية:\nSelect thermal printer type:" \
                        --radiolist --column "" --column "ID" --column "النوع / Type" \
                        TRUE  "xprinter" "🖨️  X-Printer  (XP-80 Series)" \
                        FALSE "sprt"     "🖨️  SPRT  (80mm Thermal)" \
                        --width=$WIN_W --height=$WIN_H 2>/dev/null)
                fi

                rm -f "$XP_IMG" "$SPRIT_IMG" "$RESULT_FILE"
                [ -z "$THERMAL_BRAND" ] && continue



                USB_DEV=$(sudo -u admin lpinfo -v 2>/dev/null | grep -iE 'usb:/' | awk '{print $2}' | head -1)
                [ -z "$USB_DEV" ] && USB_DEV=$(lpinfo -v 2>/dev/null | grep -iE 'usb:/' | awk '{print $2}' | head -1)

                if [ -z "$USB_DEV" ]; then
                    read WIN_W WIN_H < <(get_win_size medium)
                zenity --warning \
                        --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                        --text "⚠️ لم يتم اكتشاف طابعة USB.\n⚠️ No USB printer detected.\n\nتأكد من توصيل الطابعة وتشغيلها.\nMake sure printer is connected and powered on." \
                    --width=$WIN_W 2>/dev/null
                    continue
                fi

                THERMAL_LOG="/tmp/ita_thermal.log"
                PROG_FILE="/tmp/ita_thermal_prog.txt"
                rm -f "$THERMAL_LOG" "$PROG_FILE"

                if [ "$THERMAL_BRAND" == "xprinter" ]; then

                    PRINTER_NAME="xp80"
                    XP_URL="https://www.dropbox.com/scl/fi/9knkouz84hqeouumyk5bd/install-xp80?rlkey=gjibguc0903787o1bjnx1s89u&st=fgtg9f6a&dl=0"
                    XP_FILE="/tmp/XP-80"

                    (
                        echo "$(date): بدء تحميل XP-80" > "$THERMAL_LOG"
                        echo "10" > "$PROG_FILE"; echo "# 📥 جاري تحميل X-Printer XP-80 driver..." >> "$PROG_FILE"

                        curl -fL --connect-timeout 20 --max-time 120 --retry 2 \
                            -o "$XP_FILE" "$XP_URL" 2>>"$THERMAL_LOG"

                        if [ $? -ne 0 ] || [ ! -s "$XP_FILE" ]; then
                            # fallback direct-download
                            XP_URL="https://www.dropbox.com/scl/fi/9knkouz84hqeouumyk5bd/install-xp80?rlkey=gjibguc0903787o1bjnx1s89u&st=fgtg9f6a&dl=1"
                            curl -fL --connect-timeout 20 --max-time 120 --retry 2 \
                                -o "$XP_FILE" "$XP_URL" 2>>"$THERMAL_LOG"
                        fi

                        if [ ! -s "$XP_FILE" ]; then
                            echo "$(date): فشل التحميل" >> "$THERMAL_LOG"
                            echo "FAIL" > "$PROG_FILE"; exit 1
                        fi

                        echo "$(date): اكتمل التحميل — $(du -sh "$XP_FILE" | cut -f1)" >> "$THERMAL_LOG"
                        echo "40" > "$PROG_FILE"; echo "# ⚙️ جاري التثبيت..." >> "$PROG_FILE"

                        chmod 777 "$XP_FILE"
                        cd /tmp && ./XP-80 >>"$THERMAL_LOG" 2>&1

                        echo "72" > "$PROG_FILE"; echo "# 🔄 إعادة تشغيل CUPS..." >> "$PROG_FILE"
                        systemctl restart cups >>"$THERMAL_LOG" 2>&1
                        sleep 2

                        rm -f "$XP_FILE"
                        echo "100" > "$PROG_FILE"; echo "# ✅ اكتمل" >> "$PROG_FILE"
                        echo "DONE" >> "$PROG_FILE"
                    ) &
                    BG_PID=$!

                    read _PW _PH < <(get_win_size progress)
                    (
                        LAST_VAL=5
                        while kill -0 "$BG_PID" 2>/dev/null; do
                            [ -f "$PROG_FILE" ] && {
                                NV=$(grep '^[0-9]' "$PROG_FILE" | tail -1)
                                NM=$(grep '^#' "$PROG_FILE" | tail -1)
                                [ -n "$NV" ] && [ "$NV" != "$LAST_VAL" ] && echo "$NV" && LAST_VAL="$NV"
                                [ -n "$NM" ] && echo "$NM"
                                grep -q "^FAIL" "$PROG_FILE" 2>/dev/null && break
                            }
                            sleep 1
                        done
                        echo "100"; echo "# ✅ اكتمل / Done"
                    ) | zenity --progress \
                        --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                        --text "⚙️ جاري تثبيت تعريف X-Printer XP-80...\nInstalling X-Printer XP-80 driver..." \
                        --width=$_PW --height=$_PH --no-cancel 2>/dev/null

                    wait "$BG_PID"
                    rm -f "$PROG_FILE"


                    XP_PPD=$(lpinfo -m 2>/dev/null | grep -i "XP-80\|XP80\|xprinter" | head -1 | awk '{print $1}')

                    if [ -z "$XP_PPD" ]; then
                        read _EW _EH < <(get_win_size medium)
                        zenity --error --title "خطأ / Error" --window-icon="$SYS_ICON" \
                            --text "❌ لم يتم العثور على تعريف XP-80 بعد التثبيت.\n❌ XP-80 driver not found after install.\n\nLog: $THERMAL_LOG" \
                            --width=$_EW 2>/dev/null
                        continue
                    fi

                    read _PW _PH < <(get_win_size progress)
                    (
                        echo "20"
                        lpstat -e 2>/dev/null | grep -q "^$PRINTER_NAME$" \
                            && sudo -u admin /usr/sbin/lpadmin -x "$PRINTER_NAME" 2>/dev/null
                        echo "45"
                        sudo -u admin /usr/sbin/lpadmin \
                            -p "$PRINTER_NAME" -E \
                            -v "$USB_DEV" \
                            -m "$XP_PPD" \
                            -D "X-Printer XP-80" \
                            2>/tmp/ita_err
                        echo "65"
                        sudo -u admin /usr/sbin/cupsenable "$PRINTER_NAME" 2>/dev/null
                        sudo -u admin /usr/sbin/cupsaccept "$PRINTER_NAME" 2>/dev/null
                        echo "82"
                        set_thermal_defaults "$PRINTER_NAME"
                        echo "100"
                    ) | zenity --progress \
                        --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                        --text "🖨️ جاري إضافة X-Printer XP-80...\nAdding X-Printer XP-80..." \
                        --auto-close --width=$_PW --height=$_PH 2>/dev/null

                    read _IW _IH < <(get_win_size medium)
                    if lpstat -e 2>/dev/null | grep -q "^$PRINTER_NAME$"; then
                        zenity --info --title "نجاح / Success" --window-icon="$SYS_ICON" \
                            --text "✅ تمت إضافة X-Printer XP-80 بنجاح!\n✅ X-Printer XP-80 added!\n\n🖨️ الاسم: $PRINTER_NAME\n🔌 USB: $USB_DEV\n📄 Paper: 80mm" \
                            --width=$_IW 2>/dev/null
                    else
                        ERR_MSG=$(cat /tmp/ita_err 2>/dev/null | head -5)
                        zenity --error --title "خطأ / Error" --window-icon="$SYS_ICON" \
                            --text "❌ فشلت إضافة الطابعة.\n❌ Failed to add printer.\n\n$ERR_MSG" \
                            --width=$_IW 2>/dev/null
                    fi
                    rm -f /tmp/ita_err

                elif [ "$THERMAL_BRAND" == "sprt" ]; then

                    PRINTER_NAME="SPRT"
                    SPRIT_URL="https://www.dropbox.com/scl/fo/eoxs40b23h5g8zxk0vhnj/AGVfJEgg05my1TcWe1xHCs4?rlkey=pqx2yv4x5blqmz0vks058ef9g&st=hcp53bq0&dl=0"
                    SPRIT_ZIP="/tmp/sprit_driver.zip"
                    SPRIT_DIR="/tmp/sprit_driver_extract"

                    (
                        echo "$(date): بدء تحميل SPRT" > "$THERMAL_LOG"
                        echo "10" > "$PROG_FILE"; echo "# 📥 جاري تحميل SPRT driver..." >> "$PROG_FILE"

                        curl -fL --connect-timeout 20 --max-time 180 --retry 2 \
                            -o "$SPRIT_ZIP" "$SPRIT_URL" 2>>"$THERMAL_LOG"

                        if [ $? -ne 0 ] || [ ! -s "$SPRIT_ZIP" ]; then
                            # fallback direct-download
                            SPRIT_URL="https://www.dropbox.com/scl/fo/eoxs40b23h5g8zxk0vhnj/AGVfJEgg05my1TcWe1xHCs4?rlkey=pqx2yv4x5blqmz0vks058ef9g&st=hcp53bq0&dl=1"
                            curl -fL --connect-timeout 20 --max-time 180 --retry 2 \
                                -o "$SPRIT_ZIP" "$SPRIT_URL" 2>>"$THERMAL_LOG"
                        fi

                        if [ ! -s "$SPRIT_ZIP" ]; then
                            echo "$(date): فشل التحميل" >> "$THERMAL_LOG"
                            echo "FAIL" > "$PROG_FILE"; exit 1
                        fi

                        echo "$(date): اكتمل التحميل — $(du -sh "$SPRIT_ZIP" | cut -f1)" >> "$THERMAL_LOG"
                        echo "35" > "$PROG_FILE"; echo "# 📂 جاري فك الضغط..." >> "$PROG_FILE"

                        rm -rf "$SPRIT_DIR"; mkdir -p "$SPRIT_DIR"

                        unzip -o "$SPRIT_ZIP" -d "$SPRIT_DIR" >>"$THERMAL_LOG" 2>&1
                        UNZIP_RC=$?
                        if [ "$UNZIP_RC" -eq 0 ] || [ "$UNZIP_RC" -eq 1 ] || \
                           [ -f "$SPRIT_DIR/install.sh" ] || [ -f "$SPRIT_DIR/80mmSeries.ppd" ]; then
                            echo "$(date): فك zip ناجح (rc=$UNZIP_RC)" >> "$THERMAL_LOG"
                        elif tar -xzf "$SPRIT_ZIP" -C "$SPRIT_DIR" >>"$THERMAL_LOG" 2>&1; then
                            echo "$(date): فك tar ناجح" >> "$THERMAL_LOG"
                        else
                            echo "$(date): فشل فك الضغط" >> "$THERMAL_LOG"
                            echo "FAIL" > "$PROG_FILE"; exit 1
                        fi
                        rm -f "$SPRIT_ZIP"

                        echo "55" > "$PROG_FILE"; echo "# ⚙️ جاري تشغيل install.sh..." >> "$PROG_FILE"

                        SETUP_SH=$(find "$SPRIT_DIR" -name "setup.sh" | head -1)
                        INSTALLER=$(find "$SPRIT_DIR" -name "install.sh" | head -1)

                        if [ -n "$SETUP_SH" ]; then
                            chmod +x "$SETUP_SH"
                            echo "$(date): chmod +x setup.sh" >> "$THERMAL_LOG"
                        fi

                        if [ -n "$INSTALLER" ]; then
                            chmod +x "$INSTALLER"
                            INSTALLER_DIR=$(dirname "$INSTALLER")
                            INSTALLER_BASE=$(basename "$INSTALLER")
                            (
                                cd "$INSTALLER_DIR" || exit 1
                                ./"$INSTALLER_BASE"
                            ) >>"$THERMAL_LOG" 2>&1
                            echo "$(date): install.sh اكتمل" >> "$THERMAL_LOG"
                        else
                            echo "$(date): install.sh غير موجود — نسخ PPD يدوياً" >> "$THERMAL_LOG"
                            mkdir -p /usr/share/cups/model/SPRIT
                            find "$SPRIT_DIR" \( -name "*.ppd" -o -name "*.PPD" \) | while read -r PPD_F; do
                                cp -f "$PPD_F" /usr/share/cups/model/SPRIT/ 2>>"$THERMAL_LOG"
                            done
                        fi

                        # fallback قوي: تأكيد نسخ فلاتر CUPS + ملفات PPD حتى لو install.sh ماكملهاش
                        SERVERROOT=$(awk '/^[[:space:]]*ServerRoot[[:space:]]+/ {print $2; exit}' /etc/cups/cupsd.conf)
                        SERVERBIN=$(awk '/^[[:space:]]*ServerBin[[:space:]]+/ {print $2; exit}' /etc/cups/cupsd.conf)
                        DATADIR=$(awk '/^[[:space:]]*DataDir[[:space:]]+/ {print $2; exit}' /etc/cups/cupsd.conf)

                        if [ -z "$SERVERBIN" ]; then
                            FILTERDIR="/usr/lib/cups/filter"
                        elif [ "${SERVERBIN:0:1}" = "/" ]; then
                            FILTERDIR="$SERVERBIN/filter"
                        else
                            FILTERDIR="$SERVERROOT/$SERVERBIN/filter"
                        fi

                        if [ -z "$DATADIR" ]; then
                            PPDDIR="/usr/share/cups/model/printer"
                        elif [ "${DATADIR:0:1}" = "/" ]; then
                            PPDDIR="$DATADIR/model/printer"
                        else
                            PPDDIR="$SERVERROOT/$DATADIR/model/printer"
                        fi

                        mkdir -p "$FILTERDIR" "$PPDDIR" /usr/lib/cups/filter
                        for FNAME in rastertoprinter rastertoprinterlm rastertoprintercm; do
                            FSRC=$(find "$SPRIT_DIR" ${INSTALLER_DIR:+$INSTALLER_DIR} -type f \( -name "$FNAME" -o -name "${FNAME}.bin" \) 2>/dev/null | head -1)
                            if [ -n "$FSRC" ]; then
                                cp -f "$FSRC" "$FILTERDIR/$FNAME" 2>>"$THERMAL_LOG"
                                chmod +x "$FILTERDIR/$FNAME" 2>>"$THERMAL_LOG"
                                cp -f "$FSRC" "/usr/lib/cups/filter/$FNAME" 2>>"$THERMAL_LOG"
                                chmod +x "/usr/lib/cups/filter/$FNAME" 2>>"$THERMAL_LOG"
                            fi
                        done

                        find "$SPRIT_DIR" -type f \( -name "*.ppd.gz" -o -name "*.PPD.GZ" \) 2>/dev/null \
                            | while read -r GZPPD; do
                                cp -f "$GZPPD" "$PPDDIR/" 2>>"$THERMAL_LOG"
                            done

                        echo "78" > "$PROG_FILE"; echo "# 🔄 إعادة تشغيل CUPS..." >> "$PROG_FILE"
                        systemctl restart cups >>"$THERMAL_LOG" 2>&1
                        sleep 2

                        echo "100" > "$PROG_FILE"; echo "# ✅ اكتمل التثبيت" >> "$PROG_FILE"
                        echo "DONE" >> "$PROG_FILE"
                    ) &
                    BG_PID=$!

                    read _PW _PH < <(get_win_size progress)
                    (
                        LAST_VAL=5
                        while kill -0 "$BG_PID" 2>/dev/null; do
                            [ -f "$PROG_FILE" ] && {
                                NV=$(grep '^[0-9]' "$PROG_FILE" | tail -1)
                                NM=$(grep '^#' "$PROG_FILE" | tail -1)
                                [ -n "$NV" ] && [ "$NV" != "$LAST_VAL" ] && echo "$NV" && LAST_VAL="$NV"
                                [ -n "$NM" ] && echo "$NM"
                                grep -q "^FAIL" "$PROG_FILE" 2>/dev/null && break
                            }
                            sleep 1
                        done
                        echo "100"; echo "# ✅ اكتمل / Done"
                    ) | zenity --progress \
                        --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                        --text "⚙️ جاري تثبيت تعريف SPRT...\nInstalling SPRT driver..." \
                        --width=$_PW --height=$_PH --no-cancel 2>/dev/null

                    wait "$BG_PID"
                    rm -f "$PROG_FILE"


                    SPRIT_PPD_FILE=$(find "$SPRIT_DIR" /usr/share/cups/model /usr/share/ppd \
                        -name "80mmSeries.ppd" 2>/dev/null | head -1)
                    [ -z "$SPRIT_PPD_FILE" ] && SPRIT_PPD_FILE=$(find "$SPRIT_DIR" /usr/share/cups/model /usr/share/ppd \
                        -name "80mmSeries.ppd.gz" 2>/dev/null | head -1)
                    [ -z "$SPRIT_PPD_FILE" ] && SPRIT_PPD_FILE=$(find "$SPRIT_DIR" /usr/share/cups/model \
                        -name "*.ppd" 2>/dev/null | grep -i 'sprt\|sprit\|80mm\|thermal' | head -1)
                    [ -z "$SPRIT_PPD_FILE" ] && SPRIT_PPD_FILE=$(find "$SPRIT_DIR" /usr/share/cups/model \
                        -name "*.ppd.gz" 2>/dev/null | grep -i 'sprt\|sprit\|80mm\|thermal' | head -1)

                    if [ -z "$SPRIT_PPD_FILE" ]; then
                        read _EW _EH < <(get_win_size medium)
                        zenity --error --title "خطأ / Error" --window-icon="$SYS_ICON" \
                            --text "❌ لم يتم العثور على 80mmSeries.ppd.\n❌ PPD file not found.\n\nLog: $THERMAL_LOG" \
                        --width=$_EW 2>/dev/null
                        rm -rf "$SPRIT_DIR"
                        continue
                    fi


                    if [ ! -x "/usr/lib/cups/filter/rastertoprinter" ] && [ ! -x "$FILTERDIR/rastertoprinter" ]; then
                        read _EW _EH < <(get_win_size medium)
                        zenity --error --title "خطأ / Error" --window-icon="$SYS_ICON" \
                            --text "❌ فلتر الطباعة rastertoprinter غير موجود.\n❌ Missing rastertoprinter filter.\n\nLog: $THERMAL_LOG" \
                            --width=$_EW 2>/dev/null
                        rm -rf "$SPRIT_DIR"
                        continue
                    fi


                    mkdir -p /usr/share/cups/model/SPRIT
                    SPRIT_PPD_DEST="/usr/share/cups/model/SPRIT/80mmSeries.ppd"
                    if echo "$SPRIT_PPD_FILE" | grep -qi '\.gz$'; then
                        gzip -dc "$SPRIT_PPD_FILE" > "$SPRIT_PPD_DEST" 2>/dev/null
                    else
                        cp -f "$SPRIT_PPD_FILE" "$SPRIT_PPD_DEST" 2>/dev/null
                    fi


                    if grep -qi "FullCut\|Full Cut\|CutType\|AutoCut" "$SPRIT_PPD_DEST" 2>/dev/null; then
                        sed -i 's/\*DefaultCutType:.*/\*DefaultCutType: FullCut/gI' "$SPRIT_PPD_DEST" 2>/dev/null
                        sed -i 's/\*DefaultAutoCut:.*/\*DefaultAutoCut: FullCut/gI' "$SPRIT_PPD_DEST" 2>/dev/null
                    fi

                    systemctl restart cups 2>/dev/null; sleep 1

                    read _PW _PH < <(get_win_size progress)
                    (
                        echo "20"
                        lpstat -e 2>/dev/null | grep -q "^$PRINTER_NAME$" \
                            && sudo -u admin /usr/sbin/lpadmin -x "$PRINTER_NAME" 2>/dev/null
                        echo "45"
                        sudo -u admin /usr/sbin/lpadmin \
                            -p "$PRINTER_NAME" -E \
                            -v "$USB_DEV" \
                            -P "$SPRIT_PPD_DEST" \
                            -D "SPRT 80mm Thermal" \
                            2>/tmp/ita_err
                        echo "65"
                        sudo -u admin /usr/sbin/cupsenable "$PRINTER_NAME" 2>/dev/null
                        sudo -u admin /usr/sbin/cupsaccept "$PRINTER_NAME" 2>/dev/null
                        echo "80"
                        set_thermal_defaults "$PRINTER_NAME"
                        echo "100"
                    ) | zenity --progress \
                        --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                        --text "🖨️ جاري إضافة SPRT...\nAdding SPRT printer..." \
                        --auto-close --width=$_PW --height=$_PH 2>/dev/null

                    rm -rf "$SPRIT_DIR"

                    read _IW _IH < <(get_win_size medium)
                    if lpstat -e 2>/dev/null | grep -q "^$PRINTER_NAME$"; then
                        zenity --info --title "نجاح / Success" --window-icon="$SYS_ICON" \
                            --text "✅ تمت إضافة SPRT بنجاح!\n✅ SPRT printer added!\n\n🖨️ الاسم: $PRINTER_NAME\n🔌 USB: $USB_DEV\n📄 PPD: 80mmSeries\n✂️ Cut: Full Cut\n📄 Media: 80mm x 297mm" \
                            --width=$_IW 2>/dev/null
                    else
                        ERR_MSG=$(cat /tmp/ita_err 2>/dev/null | head -5)
                        zenity --error --title "خطأ / Error" --window-icon="$SYS_ICON" \
                            --text "❌ فشلت إضافة الطابعة.\n❌ Failed to add printer.\n\n$ERR_MSG" \
                            --width=$_IW 2>/dev/null
                    fi
                    rm -f /tmp/ita_err
                fi
            elif [ "$THERMAL_CHOICE" == "remove" ]; then
                read WIN_W WIN_H < <(get_win_size medium)

                ALL_PRINTERS=$(lpstat -e 2>/dev/null)
                if [ -z "$ALL_PRINTERS" ]; then
                    zenity --warning --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                        --text "⚠️ لا توجد طابعات مضافة في النظام.\n⚠️ No printers found in the system." \
                        --width=$WIN_W 2>/dev/null
                    continue
                fi

                ZENITY_ARGS=()
                while read -r PNAME; do
                    [ -z "$PNAME" ] && continue
                    URI=$(lpstat -v "$PNAME" 2>/dev/null | awk '{print $NF}')
                    if echo "$URI" | grep -qiE 'usb|direct|parallel'; then
                        PTYPE="🔌 USB / حرارية"
                    elif echo "$URI" | grep -qiE 'ipp|lpd|socket'; then
                        PTYPE="🌐 شبكة"
                    else
                        PTYPE="❓ أخرى"
                    fi
                    ZENITY_ARGS+=("$PNAME" "$PTYPE" "$URI")
                done <<< "$ALL_PRINTERS"

                read WIN_W WIN_H < <(get_win_size wide)
                SELECTED=$(zenity --list \
                    --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                    --text "🗑️ اختر الطابعة للحذف:\n🗑️ Select printer to remove:" \
                    --column "الاسم / Name" --column "النوع / Type" --column "العنوان / URI" \
                    --print-column=1 "${ZENITY_ARGS[@]}" \
                    --width=$WIN_W --height=$WIN_H 2>/dev/null)

                [ -z "$SELECTED" ] && continue

                read WIN_W WIN_H < <(get_win_size medium)
                zenity --question --title "تأكيد / Confirm" --window-icon="$SYS_ICON" \
                    --text "⚠️ هل أنت متأكد من حذف:\n⚠️ Confirm removal of:\n\n🖨️  $SELECTED" \
                    --ok-label="نعم، احذف / Yes, Remove" \
                    --cancel-label="إلغاء / Cancel" --width=$WIN_W 2>/dev/null

                [ $? -ne 0 ] && continue

                read _PW _PH < <(get_win_size progress)
                (
                    echo "20"

                    sudo -u admin /usr/sbin/cancel -a "$SELECTED" 2>/dev/null
                    cancel -a "$SELECTED" 2>/dev/null
                    echo "50"

                    sudo -u admin /usr/sbin/cupsdisable "$SELECTED" 2>/dev/null
                    sleep 1
                    echo "70"

                    sudo -u admin /usr/sbin/lpadmin -x "$SELECTED" 2>/tmp/ita_err
                    echo "100"
                ) | zenity --progress \
                    --title "$TOOL_NAME" --window-icon="$SYS_ICON" \
                    --text "🗑️ جاري حذف الطابعة...\nRemoving printer..." \
                    --auto-close --width=$_PW --height=$_PH 2>/dev/null

                read _IW _IH < <(get_win_size medium)
                if ! lpstat -e 2>/dev/null | grep -q "^$SELECTED$"; then
                    zenity --info --title "نجاح / Success" --window-icon="$SYS_ICON" \
                        --text "✅ تم الحذف بنجاح!\n✅ Printer removed successfully!\n\n🖨️  $SELECTED" \
                        --width=$_IW 2>/dev/null
                else
                    ERR_MSG=$(cat /tmp/ita_err 2>/dev/null | head -5)
                    zenity --error --title "خطأ / Error" --window-icon="$SYS_ICON" \
                        --text "❌ فشل الحذف.\n❌ Failed to remove printer.\n\n$ERR_MSG" \
                        --width=$_IW 2>/dev/null
                fi
                rm -f /tmp/ita_err
            fi
            ;;

        5)
            read WIN_W WIN_H < <(get_win_size medium)
            PRINTER_LIST=$(lpstat -e)
            if [ -z "$PRINTER_LIST" ]; then
                zenity --error --text "لا توجد طابعات مضافة للنظام." 2>/dev/null
            else
                SELECTED_PRINTER=$(echo "$PRINTER_LIST" | zenity --list --title "إدارة الطابعات" --text "$PRINTER_LIST_MSG" --column "اسم الطابعة" --width=$WIN_W --height=$WIN_H 2>/dev/null)
                if [ -n "$SELECTED_PRINTER" ]; then
                    read _PW _PH < <(get_win_size progress)
                    (
                    echo "30"; sudo -u admin /usr/sbin/cancel -a "$SELECTED_PRINTER" 2>/dev/null
                    echo "60"; sudo -u admin /usr/sbin/cupsenable "$SELECTED_PRINTER" 2>/dev/null
                    echo "90"; sudo -u admin /usr/sbin/cupsaccept "$SELECTED_PRINTER" 2>/dev/null
                    echo "100"
                    ) | zenity --progress --text "$ENABLE_MSG" --auto-close --width=$_PW --height=$_PH 2>/dev/null
                    read _IW _IH < <(get_win_size medium)
                    zenity --info --text "$TXT_SUCCESS\nتم تفعيل الطابعة ($SELECTED_PRINTER) ومسح الأوامر بنجاح." --width=$_IW 2>/dev/null
                fi
            fi
            ;;

        6)
            read _PW _PH < <(get_win_size progress)
            (echo "50"; systemctl stop cups; rm -rf /var/spool/cups/*; systemctl start cups; echo "100") | zenity --progress --text "$TXT_WAIT" --auto-close 2>/dev/null
            read WIN_W WIN_H < <(get_win_size medium)
            zenity --info --text "$TXT_SUCCESS" 2>/dev/null
            ;;

        7)
            read WIN_W WIN_H < <(get_win_size wide)
            STATUS=$(lpstat -p 2>/dev/null); JOBS=$(lpstat -o 2>/dev/null)
            zenity --info --text "<b>الحالة العامة:</b>\n$STATUS\n\n<b>الأوامر العالقة:</b>\n$JOBS" --width=$WIN_W 2>/dev/null
            ;;
    esac
done
