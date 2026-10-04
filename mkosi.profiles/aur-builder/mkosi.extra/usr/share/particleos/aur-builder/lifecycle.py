# SPDX-License-Identifier: LGPL-2.1-or-later
"""Root-only lifecycle broker; never reads or copies guest build artifacts."""
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import select
import shutil
import socket
import stat
import struct
import subprocess
import sys
import uuid

TEMPLATE = Path("/usr/share/particleos/aur-builder")
STATE = Path("/var/lib/particleos/aur-builder")
RUNTIME = Path("/run/particleos-aur")
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
    files = [TEMPLATE / "mkosi.conf", *(TEMPLATE / "mkosi.extra").rglob("*")]
    for path in sorted(files):
        if path.is_file():
            digest.update(str(path.relative_to(TEMPLATE)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def metadata(path):
    file = path / "complete.json"
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("Unsafe builder completion metadata; use --reset")
    data = json.loads(file.read_text())
    if not isinstance(data, dict):
        raise ValueError("Invalid completion metadata; use --reset")
    shift = data.get("shift")
    if not isinstance(shift, int) or not 524288 <= shift <= 1878982656 or shift % 65536:
        raise ValueError("Invalid user namespace mapping; use --reset")
    root = (path / "rootfs").lstat()
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != shift or root.st_gid != shift:
        raise ValueError("Builder root ownership does not match its reserved mapping; use --reset")
    return data


def range_available(shift, own=None):
    end = shift + 65536
    if any(shift <= p.pw_uid < end for p in pwd.getpwall()):
        return False
    if any(shift <= g.gr_gid < end for g in grp.getgrall()):
        return False
    for file in (Path("/etc/subuid"), Path("/etc/subgid")):
        if file.exists():
            for line in file.read_text().splitlines():
                if not line or line.startswith("#"):
                    continue
                _, start, count = line.split(":")
                start, count = int(start), int(count)
                if start < end and start + count > shift:
                    return False
    for entry in STATE.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and entry != own:
            if not (entry / "complete.json").exists():
                raise ValueError(f"Incomplete builder {entry.name}; reset it before creating another")
            other = metadata(entry)["shift"]
            if other < end and other + 65536 > shift:
                return False
    return True


def nspawn(root, machine, shift, initialize=False):
    options = [
        "/usr/bin/systemd-nspawn", "--quiet", "--settings=no",
        "--directory=" + str(root), "--machine=" + machine,
        "--register=no", "--keep-unit", "--console=pipe", "--as-pid2",
        "--private-users=" + ("pick" if initialize else f"{shift}:65536"),
        "--private-users-ownership=" + ("chown" if initialize else "off"),
        "--resolv-conf=off", "--link-journal=no",
        "--timezone=off", "--setenv=HOME=/build",
        "--setenv=PATH=/usr/bin", "--setenv=LANG=C.UTF-8",
    ]
    options += ["--private-network"] if initialize else ["--network-veth"]
    return [*options, "/usr/bin/python3", "-I", GUEST]


def create_builder(target, architecture, fingerprint, peer):
    stage = STATE / (".stage-" + uuid.uuid4().hex)
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
        # Reserve a namespace before untrusted code ever runs. The global lock
        # serializes reservations for inactive as well as active builders.
        for _ in range(8):
            machine = "paur" + uuid.uuid4().hex[:8]
            subprocess.run(
                nspawn(root, machine, None, initialize=True) + ["--initialize"],
                env=ENV, stdin=subprocess.DEVNULL, stdout=sys.stderr, check=True,
            )
            ownership = root.lstat()
            shift = ownership.st_uid
            if (524288 <= shift <= 1878982656 and shift % 65536 == 0 and
                    ownership.st_gid == shift and range_available(shift)):
                break
            # pick reuses the existing ownership; move it back to zero before
            # retrying with a new machine name. This is still a pristine tree.
            subprocess.run(
                ["/usr/bin/systemd-nspawn", "--quiet", "--settings=no",
                 "--directory=" + str(root), "--private-users=0:65536",
                 "--private-users-ownership=chown", "--private-network",
                 "--register=no", "--keep-unit", "--console=pipe",
                 "/usr/bin/true"],
                env=ENV, stdin=subprocess.DEVNULL, stdout=sys.stderr, check=True,
            )
        else:
            raise ValueError("Unable to reserve a nonoverlapping user namespace")
        root.rename(stage / "rootfs")
        for name in ("project", "output", "workspace", "cache", "packages"):
            shutil.rmtree(stage / name)
        data = {"schema": 1, "architecture": architecture, "shift": shift,
                "template": fingerprint}
        (stage / "complete.json").write_text(json.dumps(data) + "\n")
        stage.rename(target)
        return data
    finally:
        if stage.exists():
            remove_tree(stage)


def preflight():
    enforce = Path("/sys/kernel/security/ipe/enforce")
    lsm = Path("/sys/kernel/security/lsm")
    if lsm.exists() and "ipe" in lsm.read_text().strip().split(",") and not enforce.exists():
        raise ValueError("Cannot determine IPE enforcement state; administrator action is required")
    if enforce.exists() and enforce.read_text().strip() != "0":
        raise ValueError("IPE enforcement is active: unsigned guest executables may be denied. "
                         "An administrator must provide a suitable IPE policy; this helper never disables IPE.")
    for program in ("run0", "mkosi", "systemd-nspawn"):
        if not Path("/usr/bin", program).exists():
            raise ValueError(f"Required host tool missing: {program}")
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
    with lock(STATE / f".{uid}-{architecture}.lock"):
        if request["reset"]:
            with lock(STATE / ".allocation.lock"):
                remove_tree(target)
            peer.sendall(b'{"type":"reset"}\n')
            return 0
        preflight()
        fingerprint = template_digest()
        with lock(STATE / ".allocation.lock"):
            # A killed initial build cannot be reused as a complete builder.
            for stage in STATE.glob(".stage-*"):
                remove_tree(stage)
            if target.exists():
                data = metadata(target)
            else:
                data = create_builder(target, architecture, fingerprint, peer)
            if (data.get("schema") != 1 or data.get("architecture") != architecture or
                    data.get("template") != fingerprint):
                raise ValueError("Builder template changed; run particleos-aur --reset")
            if not range_available(data["shift"], own=target):
                raise ValueError("Reserved namespace now overlaps a host allocation; use --reset")
        machine = "paur" + hashlib.sha256(target.name.encode()).hexdigest()[:8]
        command = nspawn(target / "rootfs", machine, data["shift"]) + [package]
        if request["clean"]:
            command.append("--clean")
        peer.settimeout(None)
        process = subprocess.Popen(command, env=ENV, stdin=peer, stdout=peer, stderr=sys.stderr)
        poller = select.poll()
        poller.register(peer, select.POLLHUP | select.POLLERR | select.POLLRDHUP)
        try:
            while process.poll() is None:
                if poller.poll(500):
                    raise ValueError("Client disconnected; stopping its builder")
            return process.returncode
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise ValueError("Internal helper requires root and takes no arguments")
    os.umask(0o077)
    os.environ.clear()
    os.environ.update(ENV)
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
