#!/usr/bin/env bash
#
# flash.sh -- install CircuitPython and the pico_kit firmware onto the board.
#
# Run it with the Pico in BOOTSEL mode (hold BOOTSEL while plugging in). If
# CircuitPython is already installed, pass --code-only to skip straight to
# copying the firmware files.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UF2="$HERE/circuitpython.uf2"
UF2_URL="https://downloads.circuitpython.org/bin/raspberry_pi_pico/en_US/adafruit-circuitpython-raspberry_pi_pico-en_US-10.3.1.uf2"
CODE_ONLY=0
[ "${1:-}" = "--code-only" ] && CODE_ONLY=1

# Find where a labelled volume is mounted, whoever mounted it.
#
# lsblk does not always surface a FAT label (it depends on udev having probed
# it), so fall back to matching the mount point's own name -- automounters name
# the directory after the label anyway.
find_volume() {
    local want="$1" mount
    mount="$(lsblk -rno LABEL,MOUNTPOINT 2>/dev/null \
        | awk -v want="$want" '$1 == want && $2 != "" { print $2; exit }')"
    if [ -z "$mount" ]; then
        mount="$(findmnt -rno TARGET 2>/dev/null | grep -m1 -E "/${want}\$" || true)"
    fi
    [ -n "$mount" ] || return 1
    printf '%s' "$mount"
}

wait_for_volume() {
    local label="$1" timeout="$2" deadline mount
    deadline=$(( $(date +%s) + timeout ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        mount="$(find_volume "$label")"
        if [ -n "$mount" ]; then
            printf '%s' "$mount"
            return 0
        fi
        sleep 1
    done
    return 1
}

install_circuitpython() {
    local bootsel
    if ! bootsel="$(find_volume RPI-RP2)"; then
        echo "Put the board in BOOTSEL mode: unplug it, hold the BOOTSEL button,"
        echo "plug it back in while holding, then release. Waiting up to 60s..."
        bootsel="$(wait_for_volume RPI-RP2 60)" || {
            echo "error: no RPI-RP2 volume appeared." >&2
            echo "If the board is plugged in but nothing shows up, suspect a" >&2
            echo "charge-only USB cable before suspecting the board." >&2
            exit 1
        }
    fi

    if [ ! -f "$UF2" ]; then
        echo "Downloading CircuitPython..."
        curl -sSL --fail -o "$UF2" "$UF2_URL"
    fi

    echo "Installing CircuitPython to $bootsel ..."
    cp "$UF2" "$bootsel/"
    sync
    # The board reboots itself the moment the copy lands, so the drive vanishing
    # is success, not an error.
    echo "Board is rebooting into CircuitPython."
}

install_firmware() {
    local drive
    echo "Waiting for the CIRCUITPY drive..."
    drive="$(wait_for_volume CIRCUITPY 60)" || {
        echo "error: CIRCUITPY never appeared." >&2
        exit 1
    }

    echo "Copying firmware to $drive ..."
    # hid_layout and boot.py first: code.py landing last means the board only
    # soft-reloads once, with everything it needs already present.
    cp "$HERE/firmware/hid_layout.py" "$drive/"
    cp "$HERE/firmware/boot.py" "$drive/"
    cp "$HERE/firmware/code.py" "$drive/"
    sync
    echo
    echo "Done. boot.py defines the USB descriptors, which only take effect on a"
    echo "hard reset -- unplug the board and plug it back in now."
    echo "Then check it with:  python3 $HERE/host/picohid.py info"
}

[ "$CODE_ONLY" -eq 1 ] || install_circuitpython
install_firmware
