# SPDX-License-Identifier: LGPL-2.1-or-later
"""Shell orchestration tests; all administrative commands are mocked."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "mkosi.profiles/aur-builder/mkosi.extra/usr/bin/particleos-aur"
STUB = r'''#!/usr/bin/python3
import json, os, pathlib, sys
command, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
work = pathlib.Path(os.environ["WORK"])
with (work / "calls").open("a") as log:
    log.write(json.dumps([command, *args]) + "\n")
if command == "id": print("1000")
elif command == "uname": print("x86_64")
elif command == "sdme": print("sdme 0.21.0")
elif command == "sha256sum":
    filename = sys.stdin.read().strip().split("  ", 1)[1]
    sys.exit(int(os.environ.get("CACHE_BAD", "0")) or int(not pathlib.Path(filename).is_file()))
elif command == "curl":
    pathlib.Path(args[args.index("--output") + 1]).write_bytes(b"sdme")
elif command == "pacman":
    print(pathlib.Path(args[-1]).name.rsplit("-", 3)[0], "1-1")
elif command == "particleos-aur":
    if args[-1] == os.environ.get("FAIL"): sys.exit(7)
    dest = pathlib.Path(args[args.index("--output") + 1])
    (dest / (args[-1] + "-2-1-x86_64.pkg.tar.zst")).write_bytes(b"pkg")
elif command == "run0":
    args = args[args.index("sdme") + 1:]
    if args == ["ps", "--json"]:
        print('[{"name":"particleos-aur-1000-x86-64"}]' if os.environ.get("EXISTS") else "[]")
    elif args == ["fs", "ls", "--json"]: print("[]")
    elif args[0] == "exec":
        user, script = args[args.index("--user") + 1], args[args.index("-euc") + 1]
        assert user == "builder" or "makepkg" not in script
        if "--printsrcinfo" in script:
            print("pkgname = demo\npkgname = sibling\n\t"
                  + os.environ.get("SIBLING_KIND", "depends") + " = sibling>=2"
                  "\n\tdepends = zlib>=1\n\tmakedepends = git>=1\n\tdepends_aarch64 = ignored")
        elif "pacman -T" in script: sys.exit(int(os.environ.get("DEPS_FAIL", "0")))
        elif "--cleanbuild" in script: sys.exit(int(os.environ.get("BUILD_FAIL", "0")))
        elif "find " in script: print(os.environ.get("ARTIFACT", "demo-1-1-x86_64.pkg.tar.zst"))
        elif "cat --" in script: sys.stdout.buffer.write(b"package bytes")
        elif "git ls-files" in script: print("PKGBUILD")
'''

class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix=".aur-test-", dir=ROOT)
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        for command in ("id", "uname", "sdme", "run0", "sha256sum", "curl", "pacman", "particleos-aur"):
            path = self.work / command
            path.write_text(STUB)
            path.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{self.work}:/usr/bin:/bin",
                    "WORK": str(self.work), "HOME": str(self.work),
                    "XDG_STATE_HOME": str(self.work / "state"), "AUR_PACKAGES": ""}
        self.script = self.work / "helper"
        (self.work / "os-release").write_text("ID=arch\n")
        self.script.write_text(HELPER.read_text().replace("/etc/os-release", str(self.work / "os-release")))

    def run_helper(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args], cwd=self.work,
                              env={**self.env, **env}, input="yes\n", text=True,
                              capture_output=True)

    def calls(self):
        return [json.loads(line) for line in (self.work / "calls").read_text().splitlines()]

    def test_create_export_and_reuse(self):
        output = self.work / "output"
        result = self.run_helper("--output", str(output), "demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((output / "demo-1-1-x86_64.pkg.tar.zst").read_bytes(), b"package bytes")
        calls = self.calls()
        self.assertTrue(any("--hardened" in call and "--userns" in call for call in calls))
        install = next(call for call in calls if '"$@"' in " ".join(call))
        self.assertEqual(install[-2:], ["zlib", "git"])
        check = next(call for call in calls if "pacman -T" in " ".join(call))
        self.assertEqual(check[-2:], ["zlib>=1", "git>=1"])
        self.assertNotIn("sibling", install)
        self.assertNotIn("ignored", install)
        (self.work / "calls").unlink()
        self.assertEqual(self.run_helper("demo", EXISTS="1").returncode, 0)
        self.assertFalse(any("create" in call for call in self.calls()))

    def test_failure_stops_and_rejects_paths(self):
        for env in ({"BUILD_FAIL": "9"}, {"DEPS_FAIL": "127"}, {"ARTIFACT": "../escape.pkg.tar.zst"}):
            result = self.run_helper("demo", **env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stop", self.calls()[-1])
        self.assertNotEqual(self.run_helper("-bad").returncode, 0)

    def test_reset(self):
        self.assertEqual(self.run_helper("--reset", EXISTS="1").returncode, 0)
        self.assertTrue(any("rm" in call for call in self.calls()))
        self.assertFalse(any("exec" in call for call in self.calls()))

    def test_sibling_check_dependency_is_not_skipped(self):
        self.assertEqual(self.run_helper("demo", SIBLING_KIND="checkdepends").returncode, 0)
        self.assertTrue(any("sibling>=2" in c and "pacman -T" in " ".join(c) for c in self.calls()))

    def prepare(self, packages="", **env):
        run = tomllib.loads((ROOT / "mise.toml").read_text())["tasks"]["prepare"]["run"]
        self.assertEqual(run, "bash scripts/prepare.sh")
        return subprocess.run(["bash", str(ROOT / "scripts/prepare.sh")], cwd=self.work,
                              env={**self.env, "AUR_PACKAGES": packages, **env},
                              capture_output=True, text=True)

    def test_prepare_cached_sdme_builds_and_prunes(self):
        output = self.work / "mkosi.packages"
        output.mkdir()
        (output / "sdme-0.21.0-1-x86_64.pkg.tar.zst").write_bytes(b"sdme")
        (output / "demo-1-1-x86_64.pkg.tar.zst").touch()
        (output / "demo-extra-1-1-x86_64.pkg.tar.zst").touch()
        result = self.prepare("demo\nother")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((output / "demo-1-1-x86_64.pkg.tar.zst").exists())
        self.assertTrue((output / "demo-extra-1-1-x86_64.pkg.tar.zst").exists())
        self.assertTrue((output / "other-2-1-x86_64.pkg.tar.zst").exists())
        self.assertFalse(any(call[0] == "curl" for call in self.calls()))
        self.assertIn("Packages=other\n", (self.work / "mkosi.conf.d/90-aur.conf").read_text())
        self.assertEqual(self.prepare().returncode, 0)
        self.assertEqual(len(list(output.glob("*.pkg.tar.zst"))), 2)

    def test_prepare_failure_preserves_previous_and_download_checks(self):
        self.assertEqual(self.prepare("demo", CACHE_BAD="1").returncode, 1)
        calls = self.calls()
        self.assertTrue(any(call[0] == "curl" for call in calls))
        self.assertGreaterEqual(sum(call[0] == "sha256sum" for call in calls), 2)
        self.assertEqual(self.prepare("demo").returncode, 0)
        config = (self.work / "mkosi.conf.d/90-aur.conf").read_bytes()
        result = self.prepare("other bad", FAIL="bad")
        self.assertEqual(result.returncode, 7)
        self.assertEqual((self.work / "mkosi.conf.d/90-aur.conf").read_bytes(), config)
        self.assertTrue((self.work / "mkosi.packages/demo-2-1-x86_64.pkg.tar.zst").exists())
        self.assertNotEqual(self.prepare("../bad").returncode, 0)

    def test_empty_prepare_downloads_sdme_without_builder(self):
        self.assertEqual(self.prepare().returncode, 0)
        self.assertTrue(any(call[0] == "curl" for call in self.calls()))
        self.assertFalse(any(call[0] == "particleos-aur" for call in self.calls()))
        self.assertIn("aur-builder", (ROOT / "mkosi.conf").read_text())
        self.assertEqual((self.work / "mkosi.conf.d/90-aur.conf").read_text(),
                         "[Config]\nProfiles=aur-builder\n\n[Content]\n")
