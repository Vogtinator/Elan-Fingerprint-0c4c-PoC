#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ELAN 04F3:0C4C Match-on-Chip fingerprint reader driver PoC.

Usage:
    ARGV0 -h | --help
    ARGV0 reset
    ARGV0 finger_info <id>
    ARGV0 verify
    ARGV0 enrolled_count
    ARGV0 enroll (-u UD)
    ARGV0 delete <id>
    ARGV0 finger_info_all
    ARGV0 delete_all
    ARGV0 fw_ver
    ARGV0 capture <png>
    ARGV0 raw (-e EP) <hex>...

Options:
-h, --help         Show help
-e EP, --ep-in EP  Input endpoint for raw commands
-u UD. --user UD   User data for enroll command

Commands:
reset              Reset sensor
finger_info <id>   Get finger info
verify             Verify finger
enrolled_count     Get number of fingers currently enrolled
enroll             Enroll a new finger
delete <id>        Delete finger
delete_all         Delete all enrolled fingers
fw_ver             Get firmware version
capture            Capture image into a PNG file
raw                Send raw command
"""
import struct
import sys
import warnings
from collections import namedtuple
from typing import Optional

import hexdump
import usb1
from docopt import docopt
from PIL import Image

Command = namedtuple("Command", ("command", "out_len", "in_len", "ep_out", "ep_in"))

COMMANDS = {
    "fw_ver":        Command(b"\x40\x19", 2, 2, 1, 3),
    "sensor_size":   Command(b"\x00\x0c", 2, 4, 1, 3),
    "verify":        Command(b"\x40\xff\x03", 3, 2, 1, 4),
    "finger_info":   Command(b"\x40\xff\x12", 4, 64, 1, 3),
    "enrolled_num":  Command(b"\x40\xff\x04", 3, 2, 1, 3),
    "enrolled_num1": Command(b"\x40\xff\x00", 3, 2, 1, 3),
    "abort":         Command(b"\x40\xff\x02", 3, 2, 1, 3),
    "commit":        Command(b"\x40\xff\x11", 72, 2, 1, 3),
    "enroll":        Command(b"\x40\xff\x01", 7, 2, 1, 4),
    "after_enroll":  Command(b"\x40\xff\x10", 3, 3, 1, 3),
    "delete":        Command(b"\x40\xff\x13", 72, 2, 1, 3),
}

ERRORS = {
    0x41: "Move slightly downwards",
    0x42: "Move slightly to the right",
    0x43: "Move slightly upwards",
    0x44: "Move slightly to the left",
    0xfb: "Sensor is dirty or wet",
    0xfd: "Finger not enrolled",
    0xfe: "Finger area not enough",
    0xdd: "Maximum number of enrolled fingers reached"
}

ID_VENDOR = 0x04f3
ID_PRODUCT = 0x0c4c
IFACE = 0


def command(usb: usb1.USBDeviceHandle, cmdname: str, payload: bytes = b"", timeout=2000) -> bytes:
    outpayload, outlen, inlen, ep_out, ep_in = COMMANDS[cmdname]
    cmd = outpayload + payload
    if len(cmd) != outlen:
        warnings.warn(f"Wrong command size: {len(cmd)} vs {outlen}")

    usb.bulkWrite(ep_out, cmd, timeout)
    resp = usb.bulkRead(ep_in, inlen, timeout)

    if len(resp) < inlen:
        warnings.warn(f"Device replied with shorter answer: {len(cmd)} vs {inlen}")

    return resp


def get_error(byte: int) -> Optional[str]:
    # Very eyeballed
    if (byte & 0xF0) == 0:
        return None
    if byte not in ERRORS:
        return f"Unknown error {hex(byte)}"
    return ERRORS[byte]


def enroll(handle: usb1.USBDeviceHandle, user_data: bytes):
    resp = command(handle, "enrolled_num")
    error = get_error(resp[1])
    if error:
        print(f"Failed to retrieve currently enrolled fingers: {error}")
        return
    new_finger_id = enrolled = resp[1]
    print(f"Enrolled fingers: {enrolled}")

    while True:
        print("Place finger on reader")
        resp = command(handle, "verify", timeout=5000)
        error = get_error(resp[1])
        if not error:
            print(f"Finger already enrolled: {resp[1]}")
            continue
        if resp[1] != 0xfd:  # Not enrolled
            print(f"Error: {error}")
            continue
        print("Proceeding")
        break

    total_attempts = 8
    attempts_done = 0
    while attempts_done < total_attempts:
        print(f"Place finger on reader [{attempts_done + 1}/{total_attempts}]")
        payload = struct.pack("BBBB", new_finger_id, total_attempts, attempts_done, 0)
        resp = command(handle, "enroll", payload, timeout=10000)
        error = get_error(resp[1])
        if resp[1] != 0:
            print(f"Error: {error} ({hex(resp[1])})")
            if resp[1] == 0xdd:
                return  # Max fingers
            continue
        attempts_done += 1

    resp = command(handle, "after_enroll")
    print(f"Whatever this means: {resp.hex(' ')}")

    print("Committing enrolled finger")
    payload = (struct.pack("B", 0xf0 | (new_finger_id + 5)) + user_data).ljust(69, b"\x00")
    resp = command(handle, "commit", payload)
    if resp[1] == 0:
        print("Enroll successful 🎉")
    else:
        print(f"Sensor is angry: {resp.hex(' ')}")


def verify(handle: usb1.USBDeviceHandle) -> int:
    while True:
        print("Place finger on reader")
        resp = command(handle, "verify", timeout=5000)
        error = get_error(resp[1])
        if error:
            print(error)
            continue
        print(f"Recognized finger: {resp[1]}")
        return resp[1]


def get_finger_info(handle: usb1.USBDeviceHandle, finger_id: int) -> bytes:
    while True:
        resp = command(handle, "finger_info", finger_id.to_bytes(1, "little"))
        if resp[1] == 0xff:
            print("Sensor is angry, verify a finger to calm it down")
            verify(handle)
            continue
        if len(resp) == 2:
            raise IOError(f"Error: {get_error(resp[1])}")
        return resp


def delete_by_id(handle: usb1.USBDeviceHandle, fpid: int):
    resp = get_finger_info(handle, fpid)
    payload = (struct.pack("B", 0xf0 | (fpid + 5)) + resp[2:]).ljust(69, b"\x00")
    delete(handle, fpid, payload)


def delete(handle: usb1.USBDeviceHandle, fpid: int, payload: bytes):
    resp = command(handle, "delete", payload)
    error = get_error(resp[1])
    if resp[1] != 0:
        print(f"Error: {error} ({hex(resp[1])})")
    else:
        print("Deleted, finger info:")
        resp = get_finger_info(handle, fpid)
        hexdump.hexdump(resp)


def main(args):
    with usb1.USBContext() as context:
        handle = context.openByVendorIDAndProductID(ID_VENDOR, ID_PRODUCT)
        if not handle:
            raise OSError("Failed to open USB device")

        with handle.claimInterface(IFACE):
            try:
                if args["reset"]:
                    handle.resetDevice()

                elif args["verify"]:
                    verify(handle)

                elif args["fw_ver"]:
                    resp = command(handle, "fw_ver")
                    print(f"Version: {resp[0]}.{resp[1]}")

                elif args["finger_info"]:
                    finger_id = int(args["<id>"])
                    resp = get_finger_info(handle, finger_id)
                    print("Finger info:")
                    hexdump.hexdump(resp)

                elif args["finger_info_all"]:
                    for finger_id in range(10):
                        resp = get_finger_info(handle, finger_id)
                        print(f"Finger info {finger_id}:")
                        hexdump.hexdump(resp)

                elif args["enrolled_count"]:
                    resp = command(handle, "enrolled_num")
                    print(f"Enrolled fingers: {resp[1]}")

                elif args["enroll"]:
                    enroll(handle, args["--user"].encode())

                elif args["raw"]:
                    ep = int(args["--ep-in"])
                    payload = bytes(map(lambda x: int(x, 16), args["<hex>"]))
                    print(f"Sending [{len(payload)}]:")
                    hexdump.hexdump(payload)
                    print()
                    handle.bulkWrite(1, payload, timeout=5000)
                    resp = handle.bulkRead(ep, 1000, timeout=5000)
                    print(f"Received [{len(resp)}]:")
                    hexdump.hexdump(resp)

                elif args["delete"]:
                    finger_id = int(args["<id>"])
                    delete_by_id(handle, finger_id)

                elif args["delete_all"]:
                    for finger_id in range(10):
                        resp = get_finger_info(handle, finger_id)
                        if resp[-1] == 0xff:
                            print(f"Finger {finger_id} not enrolled")
                            continue
                        payload = (struct.pack("B", 0xf0 | (finger_id + 5)) + resp[2:]).ljust(69, b"\x00")
                        delete(handle, finger_id, payload)

                elif args["capture"]:
                    resp = command(handle, "sensor_size")
                    width = resp[0] + 1
                    height = resp[2] + 1
                    # Capture an image
                    handle.bulkWrite(1, bytes([0x00, 0x09]), timeout=5000)
                    resp = handle.bulkRead(2, 2 * width * height, timeout=5000)
                    img = Image.frombuffer("I;16L", (width, height), resp)

                    # Get minimum and maximum pixel values
                    minv = 2<<16-1
                    maxv = 0
                    for y in range(0, height):
                        for x in range(0, width):
                            v = img.getpixel((x, y))
                            minv = min(minv, v)
                            maxv = max(v, maxv)

                    # Convert to 8bit greyscale
                    img_8b = Image.new("L", (width, height))
                    diff = maxv - minv
                    for y in range(0, height):
                        for x in range(0, width):
                            v = img.getpixel((x, y))
                            img_8b.putpixel((x, y), int((v - minv) * 256 / diff))

                    img_8b.save(args["<png>"], "PNG")

            except Exception:
                print("Aborting")
                handle.bulkWrite(1, COMMANDS["abort"].command)
                raise


if __name__ == '__main__':
    args = docopt(__doc__.replace("ARGV0", sys.argv[0]))
    main(args)
