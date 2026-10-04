# SPDX-License-Identifier: LGPL-2.1-or-later
"""Unprivileged UI and export receiver. No host package installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import uuid

NAME = re.compile(r"[a-z0-9][a-z0-9@._+-]{0,127}\Z")
ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+:~\-]{0,254}\Z")
HELPER = "/usr/share/particleos/aur-builder/lifecycle.py"
MAX_LINE = 1024 * 1024
MAX_EXPORT = 8 * 1024**3


def safe_text(text):
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in text)


def state_directory():
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not state.is_absolute():
        state = Path.home() / ".local/state"
    return state / "particleos/aur-builder"


def preflight(reset=False):
    if platform.machine() != "x86_64":
        raise ValueError("This Arch Linux builder currently supports x86_64 hosts only")
    programs = ("run0",) if reset else ("run0", "mkosi", "systemd-nspawn")
    for program in programs:
        if not Path("/usr/bin", program).is_file():
            raise ValueError(f"Required host tool missing: {program}")
    if reset:
        return
    if sys.version_info < (3, 11):
        raise ValueError("Building requires Python >=3.11")
    enforce, lsm = Path("/sys/kernel/security/ipe/enforce"), Path("/sys/kernel/security/lsm")
    if lsm.exists() and "ipe" in lsm.read_text().strip().split(",") and not enforce.exists():
        raise ValueError("Cannot determine IPE enforcement state; administrator action is required")
    if enforce.exists() and enforce.read_text().strip() != "0":
        raise ValueError("IPE enforcement is active; an administrator must provide a suitable policy "
                         "for unsigned guest executables. This tool never disables IPE.")
    for program in programs:
        minimum, label = (26, "mkosi") if program == "mkosi" else (257, "systemd")
        try:
            result = subprocess.run(
                ["/usr/bin/" + program, "--version"], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                env={"PATH": "/usr/bin", "LANG": "C.UTF-8"},
            )
        except subprocess.CalledProcessError as error:
            raise ValueError(f"Cannot determine {program} version") from error
        version = re.search(r"(?m)^" + label + r"\s+(\d+)([^\s]*)", result.stdout)
        if (not version or int(version[1]) < minimum or
                (int(version[1]) == minimum and version[2].startswith("~"))):
            raise ValueError(f"{program} requires {label} >={minimum}")


def receive_archive(stream, destination):
    """Never use extractall: reject links, special files, paths and duplicate names."""
    names, total = set(), 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = member.name
            if (not member.isreg() or not ARTIFACT.fullmatch(name) or
                    ".pkg.tar." not in name or name in names or member.size < 0):
                raise ValueError("Unsafe package archive entry")
            total += member.size
            if total > MAX_EXPORT or len(names) >= 256:
                raise ValueError("Package export exceeds safety limits")
            names.add(name)
            source = archive.extractfile(member)
            with (destination / name).open("xb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    if not names:
        raise ValueError("Build returned no packages")
    return sorted(names)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="particleos-aur",
                                     description="Review and build an AUR package in a private Arch container")
    parser.add_argument("package", nargs="?")
    default_output = state_directory()
    parser.add_argument("--output", type=Path, default=default_output,
                        help=f"Artifact directory (default: {default_output})")
    parser.add_argument("--clean", action="store_true", help="Discard earlier guest work and clean source trees")
    parser.add_argument("--reset", action="store_true", help="Delete your persistent builder for this architecture")
    args = parser.parse_args(argv)
    if args.reset:
        if args.package or args.clean or args.output != default_output:
            parser.error("--reset cannot be combined with a package, --clean or --output")
    elif not args.package or not NAME.fullmatch(args.package):
        parser.error("a valid AUR package name is required")
    return args


def main(argv=None):
    args = parse_args(argv)
    if os.getuid() == 0:
        raise ValueError("Run particleos-aur as an ordinary user, not root")
    preflight(reset=args.reset)
    # No UID, output path, environment expansion, or user-supplied run0 options
    # cross the privileged boundary. The helper authenticates the socket peer.
    process = subprocess.Popen(
        ["/usr/bin/run0", "--pipe", "--chdir=/",
         "/usr/bin/python3", "-I", HELPER],
        stdout=subprocess.PIPE,
        env={"PATH": "/usr/bin", "TERM": "dumb", "LANG": "C.UTF-8"},
    )
    destination, stream, logs = None, None, []
    try:
        address = process.stdout.readline(MAX_LINE).decode("ascii").strip()
        if not re.fullmatch(r"/run/particleos-aur/[0-9a-f]{32}\.sock", address):
            raise ValueError("Builder failed to start (check run0 authorization and stderr)")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(address)
            stream = connection.makefile("rwb", buffering=65536)
            stream.write((json.dumps({"package": args.package, "clean": args.clean,
                                     "reset": args.reset}) + "\n").encode())
            stream.flush()
            provenance, log_bytes = None, 0
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line or len(line) > MAX_LINE:
                    raise ValueError("Builder disconnected or sent an oversized message")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Invalid builder protocol message")
                kind = event.get("type")
                if kind == "log":
                    if not isinstance(event.get("text"), str):
                        raise ValueError("Invalid log message")
                    text = safe_text(event["text"])
                    logs.append(text)
                    log_bytes += len(text)
                    if log_bytes > 64 * 1024**2:
                        raise ValueError("Build log exceeds safety limit")
                    print(text, end="", flush=True)
                elif kind == "review":
                    if not isinstance(event.get("text"), str):
                        raise ValueError("Invalid review message")
                    print(safe_text(event["text"]), flush=True)
                    print("AUR recipes run arbitrary code; containers share the host kernel.")
                    approved = input("Build this exact revision? Type yes: ") == "yes"
                    stream.write(b"yes\n" if approved else b"no\n")
                    stream.flush()
                elif kind == "provenance":
                    provenance = event.get("data")
                    if not isinstance(provenance, dict):
                        raise ValueError("Invalid or missing build provenance")
                elif kind == "archive":
                    if not isinstance(provenance, dict):
                        raise ValueError("Missing build provenance")
                    args.output.mkdir(parents=True, exist_ok=True)
                    destination = args.output / (".partial-" + uuid.uuid4().hex)
                    destination.mkdir(mode=0o700)
                    names = receive_archive(stream, destination)
                    # Drain tar padding until EOF; success also requires run0/nspawn exit 0.
                    while stream.read(65536):
                        pass
                    break
                elif kind == "reset":
                    break
                elif kind == "error":
                    raise ValueError(str(event.get("text", "Unknown builder error")))
                else:
                    raise ValueError("Unexpected builder protocol message")
        status = process.wait()
        if status:
            raise ValueError(f"Builder failed with status {status}; no output published")
        if args.reset:
            print("Persistent AUR builder reset.")
            return
        if destination is None:
            raise ValueError("No export received")
        (destination / "build.log").write_text("".join(logs))
        (destination / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        with (destination / "SHA256SUMS").open("x") as sums:
            for name in [*names, "build.log", "provenance.json"]:
                with (destination / name).open("rb") as artifact:
                    digest = hashlib.file_digest(artifact, "sha256").hexdigest()
                sums.write(f"{digest}  {name}\n")
        published = args.output / (args.package + "-" + uuid.uuid4().hex)
        destination.rename(published)
        destination = None
        print(f"Artifacts (not installed): {published}")
    except (OSError, ValueError, EOFError, KeyboardInterrupt, tarfile.TarError) as error:
        if not args.reset:
            try:
                failures = state_directory() / "failures"
                failures.mkdir(mode=0o700, parents=True, exist_ok=True)
                failed_log = failures / (uuid.uuid4().hex + ".log")
                fd = os.open(failed_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as output:
                    output.write("".join(logs))
                    output.write("\nFAILED: " + safe_text(str(error)) + "\n")
                print(f"Failure log (no artifacts published): {failed_log}", file=sys.stderr)
            except OSError as log_error:
                print(f"Could not save failure log: {safe_text(str(log_error))}", file=sys.stderr)
        raise
    finally:
        if stream is not None:
            stream.close()
        if destination is not None:
            shutil.rmtree(destination)
        if process.poll() is None:
            # Closing the socket terminates the guest protocol; run0's service
            # lifetime owns nspawn, so do not publish an interrupted export.
            process.terminate()
            process.wait()
        process.stdout.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, EOFError, KeyboardInterrupt, tarfile.TarError) as error:
        print(f"particleos-aur: {safe_text(str(error))}", file=sys.stderr)
        sys.exit(1)
