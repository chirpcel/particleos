# SPDX-License-Identifier: LGPL-2.1-or-later
"""Root-only lifecycle broker; never reads or copies guest build artifacts."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import uuid

TEMPLATE = Path("/usr/share/particleos/aur-builder")
STATE = Path("/var/lib/particleos/aur-builder")
RUNTIME = Path("/run/particleos-aur")
SDME_STATE = STATE / "sdme"
SDME_CONFIG = TEMPLATE / "sdme.conf"
ENV = {"PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8"}
NAME = re.compile(r"[a-z0-9][a-z0-9@._+-]{0,127}\Z")
GUEST = "/usr/share/particleos/aur-builder/guest.py"


def trusted_directory(path, mode=0o700):
    path.mkdir(mode=mode, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f"Unsafe root-owned directory: {path}")


def lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("Unsafe lifecycle lock")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return os.fdopen(fd, "r+")


def remove_tree(path):
    # Parents are root-only, names are generated internally, and rmtree uses
    # fd-relative traversal on Linux. Refuse any remaining host-side mounts.
    if path.parent != STATE or path.is_symlink():
        raise ValueError("Refusing unsafe reset path")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("Safe fd-relative deletion is unavailable")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount = line.split()[4]
        mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), mount)
        if mount == str(path) or mount.startswith(str(path) + "/"):
            raise ValueError("Builder still has mounted filesystems; refusing reset")
    if path.exists():
        shutil.rmtree(path)


def template_digest():
    digest = hashlib.sha256()
    files = [TEMPLATE / "mkosi.conf", TEMPLATE / "sdme.conf",
             *(TEMPLATE / "mkosi.extra").rglob("*")]
    for path in sorted(files):
        if path.is_file():
            digest.update(str(path.relative_to(TEMPLATE)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def metadata(path):
    file = path / "complete.json"
    if not file.exists():
        raise ValueError("Incomplete builder; run particleos-aur --reset")
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("Unsafe builder completion metadata; use --reset")
    data = json.loads(file.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid completion metadata; use --reset")
    return data


def sdme(*arguments):
    return ["/usr/bin/sdme", "--config=" + str(SDME_CONFIG), *arguments]


def control(*arguments):
    # Only create needs guest traversal permissions; all other work retains 077.
    options = {"umask": 0o022} if arguments[0] == "create" else {}
    return subprocess.run(sdme(*arguments), env=ENV, stdin=subprocess.DEVNULL,
                          stdout=sys.stderr, stderr=sys.stderr, check=True, **options)


def guest_command(machine, *arguments):
    # v0.21.0 exec uses systemd-run --quiet --pipe --wait and preserves its status.
    # Unlike join, it never allocates a PTY; keep stderr away from the tar stream.
    return sdme("exec", machine, "--user=root", "--",
                "/usr/bin/python3", "-I", GUEST, *arguments)


def stop_builder(machine):
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", "--property=ActiveState", "--value",
         "sdme@" + machine + ".service"],
        env=ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=sys.stderr, text=True, check=True,
    )
    if result.stdout.strip() not in ("inactive", "failed"):
        try:
            control("stop", machine)
        except subprocess.CalledProcessError:
            control("stop", "--kill", machine)


def reset_builder(target, machine):
    if (SDME_STATE / "state" / machine).exists():
        stop_builder(machine)
        control("rm", "--force", machine)
    if (SDME_STATE / "fs" / (machine + "-base")).exists():
        control("fs", "rm", "--force", machine + "-base")
    remove_tree(target)


def create_builder(target, machine, architecture, fingerprint, peer):
    target.mkdir(mode=0o700)
    stage = target / ".build"
    stage.mkdir(mode=0o700)
    try:
        project = stage / "project"
        project.mkdir()
        shutil.copy2(TEMPLATE / "mkosi.conf", project / "mkosi.conf")
        shutil.copytree(TEMPLATE / "mkosi.extra", project / "mkosi.extra")
        for name in ("output", "workspace", "cache", "packages"):
            (stage / name).mkdir()
        peer.sendall(b'{"type":"log","text":"Creating independent Arch builder...\\n"}\n')
        subprocess.run(
            ["/usr/bin/mkosi", "--directory=" + str(project),
             "--output-directory=" + str(stage / "output"),
             "--workspace-directory=" + str(stage / "workspace"),
             "--cache-directory=" + str(stage / "cache"),
             "--package-cache-dir=" + str(stage / "packages"), "build"],
            cwd=project, env=ENV, stdin=subprocess.DEVNULL, stdout=sys.stderr, check=True,
        )
        root = stage / "output/rootfs"
        if not (root / "etc/pam.d/login").is_file():
            raise ValueError("Independent Arch rootfs is missing PAM login; refusing import hooks")
        control("fs", "import", str(root),
                "--name=" + machine + "-base", "--install-packages=no")
        control("create", "--name=" + machine, "--fs=" + machine + "-base",
                "--userns", "--hardened", "--network-veth", "--storage=overlay",
                "--masked-services=", "--restart=no")
        try:
            control("start", machine)
            subprocess.run(guest_command(machine, "--initialize"), env=ENV,
                           stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, check=True)
        finally:
            stop_builder(machine)
        data = {"schema": 2, "architecture": architecture,
                "template": fingerprint}
        (target / "complete.json").write_text(json.dumps(data) + "\n")
        return data
    finally:
        shutil.rmtree(stage)


def preflight():
    enforce = Path("/sys/kernel/security/ipe/enforce")
    lsm = Path("/sys/kernel/security/lsm")
    if lsm.exists() and "ipe" in lsm.read_text().strip().split(",") and not enforce.exists():
        raise ValueError("Cannot determine IPE enforcement state; administrator action is required")
    if enforce.exists() and enforce.read_text().strip() != "0":
        raise ValueError("IPE enforcement is active: unsigned guest executables may be denied. "
                         "An administrator must provide a suitable IPE policy; this helper never disables IPE.")
    for program in ("run0", "mkosi", "sdme", "systemd-nspawn"):
        if not Path("/usr/bin", program).exists():
            raise ValueError(f"Required host tool missing: {program}")
    version = subprocess.run(
        ["/usr/bin/sdme", "--version"], env=ENV, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=sys.stderr, text=True, check=True,
    )
    if version.stdout.strip() != "sdme 0.21.0":
        raise ValueError("This builder requires sdme 0.21.0")
    subprocess.run(["/usr/bin/systemctl", "start", "systemd-networkd.service"],
                   env=ENV, stdin=subprocess.DEVNULL, stdout=sys.stderr, check=True)


def serve(peer):
    _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid == 0:
        raise ValueError("The requesting socket must belong to an ordinary user")
    pwd.getpwuid(uid)
    stream = peer.makefile("rb", buffering=0)
    line = stream.readline(4097)
    if len(line) > 4096 or not line.endswith(b"\n"):
        raise ValueError("Invalid lifecycle request")
    request = json.loads(line)
    if (not isinstance(request, dict) or set(request) != {"package", "clean", "reset"} or
            type(request["clean"]) is not bool or type(request["reset"]) is not bool):
        raise ValueError("Invalid lifecycle request fields")
    package = request["package"]
    if request["reset"]:
        if package is not None or request["clean"]:
            raise ValueError("Invalid reset request")
    elif not isinstance(package, str) or not NAME.fullmatch(package):
        raise ValueError("Invalid package name")
    architecture = platform.machine()
    if architecture != "x86_64":
        raise ValueError("This Arch Linux template currently supports x86_64 hosts only")
    for path in (Path("/var/lib/particleos"), STATE):
        trusted_directory(path)
    target = STATE / f"{uid}-{architecture}"
    machine = "paur" + hashlib.sha256(target.name.encode()).hexdigest()[:16]
    with lock(STATE / f".{uid}-{architecture}.lock"):
        if request["reset"]:
            reset_builder(target, machine)
            peer.sendall(b'{"type":"reset"}\n')
            return 0
        preflight()
        fingerprint = template_digest()
        trusted_directory(SDME_STATE)
        if target.exists():
            trusted_directory(target)
            data = metadata(target)
        else:
            data = create_builder(target, machine, architecture, fingerprint, peer)
        if (data.get("schema") != 2 or data.get("architecture") != architecture or
                data.get("template") != fingerprint):
            raise ValueError("Builder template changed; run particleos-aur --reset")
        command = guest_command(machine, package)
        if request["clean"]:
            command.append("--clean")
        peer.settimeout(None)
        process = None
        try:
            stop_builder(machine)
            control("start", machine)
            process = subprocess.Popen(command, env=ENV, stdin=peer, stdout=peer, stderr=sys.stderr)
            poller = select.poll()
            poller.register(peer, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
            while process.poll() is None:
                if poller.poll(500):
                    raise ValueError("Client disconnected; stopping its builder")
            return process.returncode
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            stop_builder(machine)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise ValueError("Internal helper requires root and takes no arguments")
    os.umask(0o077)
    os.environ.clear()
    os.environ.update(ENV)
    signal.signal(signal.SIGTERM, interrupted)
    trusted_directory(RUNTIME, 0o711)
    address = RUNTIME / (uuid.uuid4().hex + ".sock")
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            listener.bind(str(address))
            address.chmod(0o666)
            listener.listen(1)
            listener.settimeout(120)
            print(address, flush=True)
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(120)
                try:
                    return serve(peer)
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
                    peer.sendall((json.dumps({"type": "error", "text": str(error)}) + "\n").encode())
                    return 1
        finally:
            address.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f"particleos-aur lifecycle: {error}", file=sys.stderr)
        sys.exit(1)
