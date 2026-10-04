# SPDX-License-Identifier: LGPL-2.1-or-later
"""Guest-only package workflow. Recipe evaluation and export are never root."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
import uuid

NAME = re.compile(r"[a-z0-9][a-z0-9@._+-]{0,127}\Z")
ARTIFACT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+:~\-]{0,254}\Z")
DEPENDENCY = re.compile(r"([a-z0-9][a-z0-9@._+-]{0,127})(?:[<>=]{1,2}[A-Za-z0-9.+_:~\-]+)?\Z")
ENV = {"PATH": "/usr/bin", "HOME": "/build", "LANG": "C.UTF-8",
       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
       "GIT_TERMINAL_PROMPT": "0", "SRCDEST": "/build/sources"}
BUILDER = 1000
LIMIT = 8 * 1024**2


def event(kind, **fields):
    print(json.dumps({"type": kind, **fields}), flush=True)


def run(command, cwd=None, builder=False, capture=False, allowed=(0,)):
    options = {"user": BUILDER, "group": BUILDER, "extra_groups": []} if builder else {}
    process = subprocess.Popen(command, cwd=cwd, env=ENV, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options)
    output = bytearray()
    try:
        while chunk := process.stdout.read1(65536):
            if capture:
                output.extend(chunk)
                if len(output) > LIMIT:
                    raise ValueError("Guest command output exceeds safety limit")
            else:
                event("log", text=chunk.decode("utf-8", errors="replace"))
        status = process.wait()
        if status not in allowed:
            raise ValueError(f"Guest command failed ({status}): {command[0]}")
        return bytes(output), status
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def rpc_package(package):
    url = "https://aur.archlinux.org/rpc/v5/info?" + urllib.parse.urlencode({"arg[]": package})
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Oversized AUR RPC response")
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get("version") != 5 or data.get("type") != "multiinfo" or
            data.get("resultcount") != 1 or not isinstance(data.get("results"), list) or
            len(data["results"]) != 1):
        raise ValueError(f"AUR RPC did not resolve exactly one package: {package}")
    result = data["results"][0]
    if (not isinstance(result, dict) or result.get("Name") != package or
            not isinstance(result.get("PackageBase"), str) or
            not NAME.fullmatch(result["PackageBase"])):
        raise ValueError("Invalid AUR RPC package/base metadata")
    return result["PackageBase"]


def parse_srcinfo(text, package, base, architecture):
    packages, dependencies, runtime_dependencies, found_base = set(), set(), set(), None
    for line in text.splitlines():
        key, separator, value = line.strip().partition(" = ")
        if not separator:
            continue
        if key == "pkgbase":
            if found_base is not None:
                raise ValueError("Multiple pkgbase fields in generated .SRCINFO")
            found_base = value
        elif key == "pkgname":
            if not NAME.fullmatch(value):
                raise ValueError("Invalid split package name")
            packages.add(value)
        elif key in {kind + suffix for kind in ("depends", "makedepends", "checkdepends")
                     for suffix in ("", "_" + architecture)}:
            if not DEPENDENCY.fullmatch(value):
                raise ValueError(f"Unsupported dependency specification: {value}")
            if key in ("depends", "depends_" + architecture):
                runtime_dependencies.add(value)
            else:
                dependencies.add(value)
    if found_base != base or package not in packages:
        raise ValueError("Reviewed recipe package/base does not match AUR RPC")
    # Sibling runtime outputs are built together, but sibling build/check
    # dependencies must already exist before this build starts.
    dependencies.update(d for d in runtime_dependencies if DEPENDENCY.fullmatch(d)[1] not in packages)
    return sorted(dependencies), sorted(packages)


def initialize():
    try:
        account = pwd.getpwnam("builder")
        if account.pw_uid != BUILDER or account.pw_gid != BUILDER:
            raise ValueError("Unexpected guest builder UID/GID")
    except KeyError:
        run(["/usr/bin/groupadd", "--gid", str(BUILDER), "builder"])
        run(["/usr/bin/useradd", "--uid", str(BUILDER),
             "--gid", str(BUILDER), "--home-dir", "/build", "--shell", "/bin/bash", "builder"])
    Path("/build").mkdir(mode=0o700, exist_ok=True)
    os.chown("/build", BUILDER, BUILDER)
    run(["/usr/bin/pacman-key", "--init"])
    run(["/usr/bin/pacman-key", "--populate", "archlinux"])


def review_checkout(repository, revision):
    raw, _ = run(["/usr/bin/git", "ls-tree", "-rz", revision], cwd=repository, builder=True, capture=True)
    parts, total = [f"AUR revision: {revision}\n"], 0
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, path = entry.split(b"\t", 1)
        mode, kind, _ = metadata.split(b" ")
        if kind != b"blob" or mode not in (b"100644", b"100755", b"120000"):
            raise ValueError("Unsupported AUR tree entry (submodules are not built)")
        path = path.decode("utf-8")
        content, _ = run(["/usr/bin/git", "show", f"{revision}:{path}"],
                         cwd=repository, builder=True, capture=True)
        total += len(content)
        if total > 512 * 1024:
            raise ValueError("AUR review exceeds 512 KiB; inspect this recipe manually")
        parts.append(f"\n--- {path!r} (mode {mode.decode()}, sha256 "
                     f"{hashlib.sha256(content).hexdigest()}) ---\n")
        try:
            parts.append(content.decode("utf-8"))
        except UnicodeDecodeError:
            parts.append("[binary file: " + content.hex() + "]")
    text = "".join(parts)
    if len(text.encode()) > 700 * 1024:
        raise ValueError("Encoded AUR review exceeds safety limit")
    event("review", text=text)
    if sys.stdin.readline(16) != "yes\n":
        raise ValueError("Review declined; no recipe executed")


def install_dependencies(dependencies):
    if not dependencies:
        return
    missing, _ = run(["/usr/bin/pacman", "-T", *dependencies], capture=True, allowed=(0, 127))
    missing = missing.decode().splitlines()
    names = []
    for dependency in missing:
        match = DEPENDENCY.fullmatch(dependency)
        if not match:
            raise ValueError("Unexpected pacman dependency result")
        name = match[1]
        _, status = run(["/usr/bin/pacman", "-Sp", "--print-format=%n", "--", name],
                        capture=True, allowed=(0, 1))
        if status:
            raise ValueError(f"Dependency is not in official repositories: {dependency}. "
                             "Recursive AUR dependencies are deliberately unsupported.")
        names.append(name)
    if names:
        run(["/usr/bin/pacman", "-S", "--needed", "--asdeps", "--noconfirm", "--", *sorted(set(names))])
    _, status = run(["/usr/bin/pacman", "-T", *dependencies], capture=True, allowed=(0, 127))
    if status:
        raise ValueError("Official repositories do not satisfy the recipe's dependency versions")


def export_packages(repository):
    raw, _ = run(["/usr/bin/makepkg", "--packagelist"], cwd=repository, capture=True)
    paths = [Path(line) for line in raw.decode().splitlines()]
    if not paths or len(paths) > 256:
        raise ValueError("Unexpected package output list")
    opened, names = [], set()
    try:
        for path in paths:
            if not path.is_absolute():
                path = repository / path
            # Only files directly inside this fresh checkout may be exported.
            if path.parent != repository or not ARTIFACT.fullmatch(path.name) or ".pkg.tar." not in path.name:
                raise ValueError("Unsafe makepkg output path")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.name in names:
                os.close(fd)
                raise ValueError("Package output is not a unique regular file")
            names.add(path.name)
            opened.append((path.name, os.fdopen(fd, "rb"), info.st_size))
        event("archive")
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
            for name, source, size in opened:
                member = tarfile.TarInfo(name)
                member.size, member.mode = size, 0o644
                archive.addfile(member, source)
    finally:
        for _, source, _ in opened:
            source.close()


def build_packages(repository, clean):
    command = ["/usr/bin/makepkg", "--noconfirm", "--nodeps"]
    if clean:
        command.append("--cleanbuild")
    run(command, cwd=repository)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", nargs="?")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--initialize", action="store_true")
    args = parser.parse_args()
    if os.getuid() != 0:
        raise ValueError("Guest workflow must begin as namespace root")
    if args.initialize:
        initialize()
        return
    if not args.package or not NAME.fullmatch(args.package):
        raise ValueError("Invalid package name")
    # A standalone nspawn command does not boot networkd. Obtain a lease
    # explicitly, using only the private veth and the profile's DHCP server.
    Path("/etc/resolv.conf").unlink(missing_ok=True)
    Path("/etc/resolv.conf").write_text("")
    run(["/usr/bin/dhcpcd", "--waitip=4", "--timeout=60", "host0"])
    run(["/usr/bin/pacman", "-Syu", "--noconfirm"])
    base = rpc_package(args.package)
    if args.clean:
        run(["/usr/bin/rm", "-rf", "--", "/build/work"], builder=True)
    work = Path("/build/work")
    run(["/usr/bin/mkdir", "-p", "--", str(work), ENV["SRCDEST"]], builder=True)
    repository = work / uuid.uuid4().hex
    run(["/usr/bin/git", "clone", "--depth=1", "--",
         f"https://aur.archlinux.org/{base}.git", str(repository)], builder=True)
    revision, _ = run(["/usr/bin/git", "rev-parse", "HEAD"], cwd=repository, builder=True, capture=True)
    revision = revision.decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("Invalid AUR git revision")
    review_checkout(repository, revision)
    generated, _ = run(["/usr/bin/makepkg", "--printsrcinfo"], cwd=repository, builder=True, capture=True)
    dependencies, packages = parse_srcinfo(generated.decode(), args.package, base, os.uname().machine)
    install_dependencies(dependencies)
    installed, _ = run(["/usr/bin/pacman", "-Q"], capture=True)
    # Irreversible drop: makepkg, packagelist evaluation and all artifact reads
    # below run as builder, with no sudo rule and no supplementary groups.
    os.setgroups([])
    os.setgid(BUILDER)
    os.setuid(BUILDER)
    os.environ.clear()
    os.environ.update(ENV)
    build_packages(repository, args.clean)
    event("provenance", data={
        "requested_package": args.package, "package_base": base,
        "split_packages": packages, "aur_url": f"https://aur.archlinux.org/{base}.git",
        "git_revision": revision, "architecture": os.uname().machine,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "srcinfo": generated.decode(), "official_packages": installed.decode().splitlines(),
        "clean": args.clean,
    })
    export_packages(repository)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        event("error", text=str(error))
        sys.exit(1)
