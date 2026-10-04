# SPDX-License-Identifier: LGPL-2.1-or-later
"""Focused stdlib tests; no privileged services or network access required."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import tarfile
import unittest
from unittest.mock import Mock, patch
import uuid

PROFILE = Path(__file__).resolve().parents[1]
CODE = PROFILE / "mkosi.extra/usr/share/particleos/aur-builder"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = load("client", CODE / "client.py")
lifecycle = load("lifecycle", CODE / "lifecycle.py")
guest = load("guest", CODE / "mkosi.extra/usr/share/particleos/aur-builder/guest.py")


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, kind, data in entries:
            item = tarfile.TarInfo(name)
            item.type = kind
            item.linkname = "/etc/shadow"
            item.size = len(data) if kind == tarfile.REGTYPE else 0
            output.addfile(item, io.BytesIO(data) if item.size else None)
    stream.seek(0)
    return stream


class ScratchTest(unittest.TestCase):
    def setUp(self):
        self.scratch = Path.cwd() / (".aur-test-" + uuid.uuid4().hex)
        self.scratch.mkdir(mode=0o700)

    def tearDown(self):
        shutil.rmtree(self.scratch)


class ExportTests(ScratchTest):
    def test_regular_files_only_and_user_owned(self):
        name = "some-package-1.2RC1-1-x86_64.pkg.tar.zst"
        result = client.receive_archive(archive([(name, tarfile.REGTYPE, b"package")]), self.scratch)
        self.assertEqual(result, [name])
        self.assertEqual((self.scratch / name).read_bytes(), b"package")
        self.assertEqual((self.scratch / name).stat().st_uid, os.getuid())

    def test_reject_unsafe_entries(self):
        for name, kind in [
            ("../escape.pkg.tar.zst", tarfile.REGTYPE),
            ("/absolute.pkg.tar.zst", tarfile.REGTYPE),
            ("nested/file.pkg.tar.zst", tarfile.REGTYPE),
            ("link.pkg.tar.zst", tarfile.SYMTYPE),
            ("hard.pkg.tar.zst", tarfile.LNKTYPE),
            ("fifo.pkg.tar.zst", tarfile.FIFOTYPE),
            ("device.pkg.tar.zst", tarfile.CHRTYPE),
            ("directory.pkg.tar.zst", tarfile.DIRTYPE),
            ("not-a-package", tarfile.REGTYPE),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                client.receive_archive(archive([(name, kind, b"bad")]), self.scratch)
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_reject_duplicate_names(self):
        name = "duplicate.pkg.tar.zst"
        with self.assertRaises(ValueError):
            client.receive_archive(archive([(name, tarfile.REGTYPE, b"a"),
                                           (name, tarfile.REGTYPE, b"b")]), self.scratch)

    def test_reject_empty_archive(self):
        with self.assertRaises(ValueError):
            client.receive_archive(archive([]), self.scratch)

    def test_reject_oversized_export(self):
        with patch.object(client, "MAX_EXPORT", 1), self.assertRaises(ValueError):
            client.receive_archive(archive([("big.pkg.tar.zst", tarfile.REGTYPE, b"xx")]), self.scratch)

    def test_guest_rejects_symlink_output_before_archive(self):
        package = self.scratch / "link.pkg.tar.zst"
        package.symlink_to("missing")
        with patch.object(guest, "run", return_value=(str(package).encode(), 0)), \
                patch.object(guest, "event") as emit, self.assertRaises(OSError):
            guest.export_packages(self.scratch)
        emit.assert_not_called()

    def run_client(self, status, protocol=None, failure="no output published"):
        class Duplex(io.BytesIO):
            def write(self, data):
                self.request = data
                return len(data)

        data = archive([("example-1-1-x86_64.pkg.tar.zst", tarfile.REGTYPE, b"package")]).read()
        if protocol is None:
            protocol = (b'{"type":"log","text":"fresh log\\n"}\n'
                        b'{"type":"provenance","data":{"git_revision":"reviewed"}}\n'
                        b'{"type":"archive"}\n')
        stream = Duplex(protocol + data)
        connection = Mock()
        connection.makefile.return_value = stream
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        process = Mock()
        process.stdout = io.BytesIO(b"/run/particleos-aur/" + b"a" * 32 + b".sock\n")
        process.wait.return_value = status
        process.poll.return_value = status
        with patch.object(client.os, "getuid", return_value=1000), \
                patch.object(client, "preflight"), \
                patch.object(client, "state_directory", return_value=self.scratch / "state"), \
                patch.object(client.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(client.socket, "socket", return_value=connection), \
                contextlib.redirect_stdout(io.StringIO()):
            if status:
                with self.assertRaisesRegex(ValueError, failure):
                    client.main(["example", "--output", str(self.scratch)])
            else:
                client.main(["example", "--output", str(self.scratch)])
        self.assertNotIn("--setenv", " ".join(spawn.call_args.args[0]))
        self.assertEqual(json.loads(stream.request), {"package": "example", "clean": False, "reset": False})

    def test_client_failure_does_not_publish_or_leave_partial_files(self):
        self.run_client(1)
        self.assertEqual([path.name for path in self.scratch.iterdir()], ["state"])
        failed, = (self.scratch / "state/failures").iterdir()
        self.assertIn("fresh log\n", failed.read_text())
        self.assertIn("FAILED:", failed.read_text())
        self.assertEqual(failed.stat().st_mode & 0o777, 0o600)

    def test_client_success_publishes_fresh_log_provenance_and_checksums(self):
        self.run_client(0)
        result, = self.scratch.iterdir()
        self.assertTrue(result.name.startswith("example-"))
        self.assertEqual((result / "build.log").read_text(), "fresh log\n")
        self.assertEqual(json.loads((result / "provenance.json").read_text()), {"git_revision": "reviewed"})
        self.assertEqual(len((result / "SHA256SUMS").read_text().splitlines()), 3)

    def test_missing_provenance_is_a_handled_protocol_error(self):
        self.run_client(1, protocol=b'{"type":"provenance"}\n', failure="missing build provenance")
        self.assertEqual([path.name for path in self.scratch.iterdir()], ["state"])

    def test_guest_rejects_output_outside_checkout(self):
        with patch.object(guest, "run", return_value=(b"/etc/shadow.pkg.tar.zst", 0)), \
                patch.object(guest, "event") as emit, self.assertRaises(ValueError):
            guest.export_packages(self.scratch)
        emit.assert_not_called()


class MetadataTests(unittest.TestCase):
    def test_split_package_and_arch_dependencies(self):
        source = """pkgbase = example
makedepends = compiler>=2
checkdepends = test-tool
depends_x86_64 = native
depends_aarch64 = other
pkgname = example
depends = example-libs
pkgname = example-libs
depends = library
"""
        dependencies, packages = guest.parse_srcinfo(source, "example-libs", "example", "x86_64")
        self.assertEqual(dependencies, ["compiler>=2", "library", "native", "test-tool"])
        self.assertEqual(packages, ["example", "example-libs"])

    def test_only_runtime_sibling_dependencies_are_excluded(self):
        source = """pkgbase = example
depends = example-libs>=2
makedepends = example-compiler
checkdepends_x86_64 = example-tests
pkgname = example
pkgname = example-libs
pkgname = example-compiler
pkgname = example-tests
"""
        dependencies, _ = guest.parse_srcinfo(source, "example", "example", "x86_64")
        self.assertEqual(dependencies, ["example-compiler", "example-tests"])

    def test_dependency_retained_when_runtime_and_build_kinds_overlap(self):
        source = """pkgbase = example
depends = example-libs>=2
makedepends = example-libs>=2
pkgname = example
pkgname = example-libs
"""
        dependencies, _ = guest.parse_srcinfo(source, "example", "example", "x86_64")
        self.assertEqual(dependencies, ["example-libs>=2"])

    def test_sibling_build_dependency_requires_official_repository(self):
        source = """pkgbase = example
makedepends = example-libs
pkgname = example
pkgname = example-libs
"""
        dependencies, _ = guest.parse_srcinfo(source, "example", "example", "x86_64")
        with patch.object(guest, "run", side_effect=[(b"example-libs\n", 127), (b"", 1)]):
            with self.assertRaisesRegex(ValueError, "Recursive AUR dependencies"):
                guest.install_dependencies(dependencies)

    def test_build_skips_only_redundant_makepkg_dependency_checks(self):
        with patch.object(guest, "run") as command:
            guest.build_packages(Path("/build/checkout"), clean=True)
        self.assertEqual(command.call_args.args[0],
                         ["/usr/bin/makepkg", "--noconfirm", "--nodeps", "--cleanbuild"])
        self.assertEqual(command.call_args.kwargs["cwd"], Path("/build/checkout"))

    def test_invalid_generated_metadata(self):
        for source in ["pkgbase = different\npkgname = example",
                       "pkgbase = example\npkgname = different",
                       "pkgbase = example\npkgbase = example\npkgname = example",
                       "pkgbase = example\npkgname = example\ndepends = --root=/host",
                       "pkgbase = example\npkgname = example\ndepends = bad;command"]:
            with self.subTest(source=source), self.assertRaises(ValueError):
                guest.parse_srcinfo(source, "example", "example", "x86_64")

    def test_rpc_validates_requested_name_and_base(self):
        valid = {"version": 5, "type": "multiinfo", "resultcount": 1,
                 "results": [{"Name": "example-libs", "PackageBase": "example"}]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(valid).encode()
        with patch.object(guest.urllib.request, "urlopen", return_value=response):
            self.assertEqual(guest.rpc_package("example-libs"), "example")
            valid["results"][0]["PackageBase"] = "../outside"
            response.read.return_value = json.dumps(valid).encode()
            with self.assertRaises(ValueError):
                guest.rpc_package("example-libs")
            valid["results"][0] = {"Name": "other", "PackageBase": "example"}
            response.read.return_value = json.dumps(valid).encode()
            with self.assertRaises(ValueError):
                guest.rpc_package("example-libs")

    def test_unsupported_aur_dependency_fails_without_install(self):
        with patch.object(guest, "run", side_effect=[(b"aur-only>=2\n", 127), (b"", 1)]) as command:
            with self.assertRaisesRegex(ValueError, "Recursive AUR dependencies"):
                guest.install_dependencies(["aur-only>=2"])
            self.assertEqual(command.call_count, 2)
            self.assertNotIn("-S", command.call_args.args[0])

    def test_official_dependency_install_and_version_check(self):
        with patch.object(guest, "run", side_effect=[
                (b"compiler>=2\n", 127), (b"compiler\n", 0), (b"", 0), (b"", 0)]) as command:
            guest.install_dependencies(["compiler>=2"])
        self.assertEqual(command.call_args_list[2].args[0],
                         ["/usr/bin/pacman", "-S", "--needed", "--asdeps", "--noconfirm", "--", "compiler"])

    def test_review_refusal_executes_no_recipe(self):
        with patch.object(guest, "run", side_effect=[
                (b"100644 blob abc\tPKGBUILD\0", 0), (b"pkgname=example\n", 0)]) as command, \
                patch.object(guest, "event"), patch.object(guest.sys, "stdin", io.StringIO("no\n")):
            with self.assertRaisesRegex(ValueError, "Review declined"):
                guest.review_checkout(Path("/build/checkout"), "a" * 40)
            self.assertTrue(all(call.args[0][0] == "/usr/bin/git" for call in command.call_args_list))

    def test_review_displays_all_tracked_hooks_and_metadata(self):
        files = ["PKGBUILD", ".SRCINFO", "example.install", "fix.patch"]
        tree = b"".join(b"100644 blob abc\t" + name.encode() + b"\0" for name in files)
        outputs = [(tree, 0), *[(("content of " + name).encode(), 0) for name in files]]
        with patch.object(guest, "run", side_effect=outputs), \
                patch.object(guest, "event") as emit, \
                patch.object(guest.sys, "stdin", io.StringIO("yes\n")):
            guest.review_checkout(Path("/build/checkout"), "a" * 40)
        text = emit.call_args.kwargs["text"]
        for name in files:
            self.assertIn(name, text)
            self.assertIn("content of " + name, text)

    def test_initialize_populates_official_keyring(self):
        with patch.object(guest.pwd, "getpwnam", return_value=Mock(pw_uid=1000, pw_gid=1000)), \
                patch.object(guest, "Path"), patch.object(guest.os, "chown"), \
                patch.object(guest, "run") as command:
            guest.initialize()
        self.assertEqual([call.args[0] for call in command.call_args_list],
                         [["/usr/bin/pacman-key", "--init"],
                          ["/usr/bin/pacman-key", "--populate", "archlinux"]])

    def test_recipe_commands_use_unprivileged_identity(self):
        process = Mock()
        process.stdout.read1.return_value = b""
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(guest.subprocess, "Popen", return_value=process) as spawn:
            guest.run(["/usr/bin/makepkg", "--printsrcinfo"], builder=True)
        self.assertEqual(spawn.call_args.kwargs["user"], 1000)
        self.assertEqual(spawn.call_args.kwargs["group"], 1000)
        self.assertEqual(spawn.call_args.kwargs["extra_groups"], [])
        self.assertNotIn("sudo", spawn.call_args.args[0])


class LifecycleTests(ScratchTest):
    def test_nspawn_mapping_and_no_host_binds(self):
        command = lifecycle.nspawn(Path("/state/rootfs"), "paur12345678", 655360)
        for expected in ["--settings=no", "--private-users=655360:65536",
                         "--private-users-ownership=off", "--network-veth", "--resolv-conf=off", "--as-pid2"]:
            self.assertIn(expected, command)
        self.assertFalse(any(value.startswith(("--bind", "--image", "--boot")) for value in command))
        initial = lifecycle.nspawn(Path("/stage/rootfs"), "paur12345678", None, initialize=True)
        self.assertIn("--private-users=pick", initial)
        self.assertIn("--private-network", initial)

    def test_reset_rejects_paths_outside_state(self):
        with patch.object(lifecycle, "STATE", self.scratch):
            with self.assertRaisesRegex(ValueError, "unsafe reset"):
                lifecycle.remove_tree(self.scratch.parent)

    def test_reset_rejects_symlink(self):
        link = self.scratch / "builder"
        link.symlink_to(self.scratch.parent, target_is_directory=True)
        with patch.object(lifecycle, "STATE", self.scratch):
            with self.assertRaisesRegex(ValueError, "unsafe reset"):
                lifecycle.remove_tree(link)

    def test_root_client_is_rejected_by_kernel_peer_identity(self):
        peer = Mock()
        peer.getsockopt.return_value = struct.pack("3i", 123, 0, 0)
        with patch.dict(os.environ, {"SUDO_UID": "1000"}):
            with self.assertRaisesRegex(ValueError, "ordinary user"):
                lifecycle.serve(peer)
        peer.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)

    def test_caller_cannot_supply_uid_or_extra_arguments(self):
        peer = Mock()
        peer.getsockopt.return_value = struct.pack("3i", 123, 1000, 1000)
        peer.makefile.return_value = io.BytesIO(
            b'{"package":"example","clean":false,"reset":false,"uid":2000}\n')
        with patch.object(lifecycle.pwd, "getpwuid"):
            with self.assertRaisesRegex(ValueError, "request fields"):
                lifecycle.serve(peer)

    def test_cli_reset_and_package_validation(self):
        self.assertTrue(client.parse_args(["--reset"]).reset)
        self.assertEqual(client.parse_args(["example-libs", "--clean"]).package, "example-libs")
        for args in [["../outside"], ["--reset", "example"], ["--reset", "--clean"],
                     ["--reset", "--output", "/host"]]:
            with self.subTest(args=args), patch.object(client.sys, "stderr", io.StringIO()), \
                    self.assertRaises(SystemExit):
                client.parse_args(args)

    def test_terminal_controls_are_escaped(self):
        self.assertEqual(client.safe_text("\x1b[31mtest\r\n"), "\\x1b[31mtest\\x0d\n")

    def test_help_never_elevates(self):
        with patch.object(client.subprocess, "Popen") as spawn, \
                contextlib.redirect_stdout(io.StringIO()) as output, \
                self.assertRaises(SystemExit) as status:
            client.main(["--help"])
        self.assertEqual(status.exception.code, 0)
        self.assertIn("--reset", output.getvalue())
        spawn.assert_not_called()

    def test_xdg_state_default_and_relative_path_fallback(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.scratch)}):
            self.assertEqual(client.parse_args(["example"]).output, self.scratch / "particleos/aur-builder")
        with patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}), \
                patch.object(client.Path, "home", return_value=self.scratch):
            self.assertEqual(client.state_directory(), self.scratch / ".local/state/particleos/aur-builder")

    def test_preflight_failure_precedes_authorization(self):
        with patch.object(client.os, "getuid", return_value=1000), \
                patch.object(client, "preflight", side_effect=ValueError("unsupported host")), \
                patch.object(client.subprocess, "Popen") as spawn, self.assertRaises(ValueError):
            client.main(["example"])
        spawn.assert_not_called()

    def test_preflight_rejects_unsupported_architecture(self):
        with patch.object(client.platform, "machine", return_value="aarch64"), \
                self.assertRaisesRegex(ValueError, "x86_64"):
            client.preflight()

    def test_preflight_rejects_missing_tool(self):
        with patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.Path, "is_file", return_value=False), \
                self.assertRaisesRegex(ValueError, "Required host tool missing"):
            client.preflight()

    def test_reset_preflight_does_not_require_build_tools_python_or_ipe(self):
        with patch.object(client.Path, "is_file", return_value=True), \
                patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.sys, "version_info", (3, 10)), \
                patch.object(client.Path, "exists", side_effect=AssertionError("IPE must not be checked")), \
                patch.object(client.subprocess, "run") as command:
            client.preflight(reset=True)
        command.assert_not_called()

    def test_preflight_rejects_old_python(self):
        with patch.object(client.Path, "is_file", return_value=True), \
                patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.sys, "version_info", (3, 10)), self.assertRaisesRegex(ValueError, "Python"):
            client.preflight()

    def test_preflight_rejects_old_tool_version(self):
        with patch.object(client.Path, "is_file", return_value=True), \
                patch.object(client.Path, "exists", return_value=False), \
                patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.subprocess, "run", return_value=Mock(stdout="systemd 256\n")), \
                self.assertRaisesRegex(ValueError, "systemd >=257"):
            client.preflight()

    def test_preflight_rejects_ipe_enforcement(self):
        with patch.object(client.Path, "is_file", return_value=True), \
                patch.object(client.Path, "exists", return_value=True), \
                patch.object(client.Path, "read_text", return_value="1"), \
                patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.subprocess, "run") as command, \
                self.assertRaisesRegex(ValueError, "IPE enforcement"):
            client.preflight()
        command.assert_not_called()

    def test_preflight_accepts_compatible_versions(self):
        versions = [Mock(stdout="systemd 257\n"), Mock(stdout="mkosi 26\n"), Mock(stdout="systemd 257\n")]
        with patch.object(client.Path, "is_file", return_value=True), \
                patch.object(client.Path, "exists", return_value=False), \
                patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.subprocess, "run", side_effect=versions):
            client.preflight()

    def test_guest_definition_digest_ignores_host_ui_changes(self):
        (self.scratch / "mkosi.conf").write_text("guest configuration")
        (self.scratch / "mkosi.extra").mkdir()
        recipe = self.scratch / "mkosi.extra/guest.py"
        recipe.write_text("guest workflow")
        (self.scratch / "client.py").write_text("host UI")
        with patch.object(lifecycle, "TEMPLATE", self.scratch):
            original = lifecycle.template_digest()
            (self.scratch / "client.py").write_text("new host UI")
            self.assertEqual(original, lifecycle.template_digest())
            recipe.write_text("updated guest")
            self.assertNotEqual(original, lifecycle.template_digest())

    def test_download_cache_is_persistent_guest_only_environment(self):
        self.assertEqual(guest.ENV["SRCDEST"], "/build/sources")
        self.assertEqual(guest.ENV["HOME"], "/build")


if __name__ == "__main__":
    unittest.main()
