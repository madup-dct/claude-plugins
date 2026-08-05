#!/usr/bin/env python3
"""Copy private boundary files through a single validated file descriptor."""

from __future__ import annotations

import argparse
import errno
import os
import stat
import sys


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def read_from_verified_fd(label: str, source_path: str, max_bytes: int) -> bytes:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        fail("Secure snapshotting is unavailable on this platform")

    flags = os.O_RDONLY | os.O_CLOEXEC | nofollow
    try:
        fd = os.open(source_path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            fail(f"{label} must not be a symlink")
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            fail(f"{label} is missing or unreadable")
        fail(f"Unable to read {label}")

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            fail(f"{label} must be a regular file")
        if metadata.st_uid != os.getuid():
            fail(f"{label} must be owned by the current user")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            fail(f"{label} must use mode 0600")
        if metadata.st_size > max_bytes:
            fail(f"{label} exceeds the maximum allowed size")

        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"{label} exceeds the maximum allowed size")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_snapshot(label: str, destination_path: str, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    try:
        fd = os.open(destination_path, flags, 0o600)
    except FileExistsError:
        fail(f"{label} snapshot destination already exists")
    except OSError:
        fail(f"Unable to create {label} snapshot")

    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
    finally:
        os.close(fd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--snapshot",
        dest="snapshots",
        metavar=("LABEL", "SOURCE", "DESTINATION", "MAX_BYTES"),
        nargs=4,
        action="append",
        required=True,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    for label, source_path, destination_path, max_bytes_raw in args.snapshots:
        try:
            max_bytes = int(max_bytes_raw)
        except ValueError:
            fail(f"{label} maximum size is invalid")
        if max_bytes <= 0:
            fail(f"{label} maximum size is invalid")

        payload = read_from_verified_fd(label, source_path, max_bytes)
        write_snapshot(label, destination_path, payload)


if __name__ == "__main__":
    main()
