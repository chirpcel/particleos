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
import subprocess
import tarfile
import tomllib
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
    def test_sdme_exec_non_pty_and_explicit_guest_root(self):
        command = lifecycle.guest_command("paur12345678", "example", "--clean")
        self.assertEqual(command, [
            "/usr/bin/sdme", "--config=" + str(lifecycle.SDME_CONFIG),
            "exec", "paur12345678", "--user=root", "--",
            "/usr/bin/python3", "-I", lifecycle.GUEST, "example", "--clean",
        ])
        self.assertFalse(any(value.startswith(("--bind", "--pty")) for value in command))

    def test_control_stdout_cannot_pollute_export(self):
        with patch.object(lifecycle.subprocess, "run") as command:
            lifecycle.control("start", "paur12345678")
        self.assertIs(command.call_args.kwargs["stdout"], lifecycle.sys.stderr)
        self.assertIs(command.call_args.kwargs["stderr"], lifecycle.sys.stderr)
        self.assertIs(command.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(command.call_args.kwargs["check"])
        self.assertNotIn("umask", command.call_args.kwargs)

    def test_only_sdme_create_relaxes_child_umask(self):
        with patch.object(lifecycle.subprocess, "run") as command:
            lifecycle.control("create", "--name=paur12345678")
            self.assertEqual(command.call_args.kwargs["umask"], 0o022)
            for operation in ("fs", "start", "stop", "rm"):
                lifecycle.control(operation, "paur12345678")
                self.assertNotIn("umask", command.call_args.kwargs)

    def test_sdme_imports_only_independent_guest_and_initializes(self):
        target = self.scratch / "1000-x86_64"

        def run_command(command, **kwargs):
            if command[0] == "/usr/bin/mkosi":
                pam = target / ".build/output/rootfs/etc/pam.d"
                pam.mkdir(parents=True)
                (pam / "login").write_text("trusted package PAM configuration")
            return Mock(stdout="active\n")

        with patch.object(lifecycle, "TEMPLATE", CODE), \
                patch.object(lifecycle.subprocess, "run", side_effect=run_command) as run, \
                patch.object(lifecycle, "control") as control:
            data = lifecycle.create_builder(target, "paur12345678", "x86_64", "digest", Mock())
        self.assertEqual(data, {"schema": 2, "architecture": "x86_64", "template": "digest"})
        commands = [call.args for call in control.call_args_list]
        self.assertEqual(commands[:3], [
            ("fs", "import", str(target / ".build/output/rootfs"),
             "--name=paur12345678-base", "--install-packages=no"),
            ("create", "--name=paur12345678", "--fs=paur12345678-base",
             "--userns", "--hardened", "--network-veth", "--storage=overlay", "--masked-services=", "--restart=no"),
            ("start", "paur12345678"),
        ])
        self.assertEqual(commands[3], ("stop", "paur12345678"))
        self.assertEqual(run.call_args_list[0].args[0][0], "/usr/bin/mkosi")
        self.assertIn("--initialize", run.call_args_list[1].args[0])
        self.assertFalse((target / ".build").exists())
        self.assertFalse((target / "rootfs").exists())
        self.assertEqual(json.loads((target / "complete.json").read_text()), data)

    def test_missing_guest_pam_refuses_sdme_import_compatibility_hooks(self):
        target = self.scratch / "1000-x86_64"
        with patch.object(lifecycle, "TEMPLATE", CODE), \
                patch.object(lifecycle.subprocess, "run"), \
                patch.object(lifecycle, "control") as control, \
                self.assertRaisesRegex(ValueError, "missing PAM login"):
            lifecycle.create_builder(target, "paur12345678", "x86_64", "digest", Mock())
        control.assert_not_called()
        self.assertFalse((target / ".build").exists())
        self.assertFalse((target / "complete.json").exists())

    def test_stop_escalates_to_sdme_kill(self):
        with patch.object(lifecycle.subprocess, "run", return_value=Mock(stdout="active\n")), \
                patch.object(lifecycle, "control",
                             side_effect=[subprocess.CalledProcessError(1, ["sdme"]), None]) as control:
            lifecycle.stop_builder("paur12345678")
        self.assertEqual([call.args for call in control.call_args_list],
                         [("stop", "paur12345678"), ("stop", "--kill", "paur12345678")])

    def test_stop_skips_inactive_guest(self):
        for state in ("inactive", "failed"):
            with self.subTest(state=state), \
                    patch.object(lifecycle.subprocess, "run", return_value=Mock(stdout=state)), \
                    patch.object(lifecycle, "control") as control:
                lifecycle.stop_builder("paur12345678")
            control.assert_not_called()

    def test_reset_removes_sdme_container_before_imported_base(self):
        target, state = self.scratch / "1000-x86_64", self.scratch / "sdme"
        target.mkdir()
        (state / "state").mkdir(parents=True)
        (state / "state/paur12345678").touch()
        (state / "fs/paur12345678-base").mkdir(parents=True)
        with patch.object(lifecycle, "STATE", self.scratch), \
                patch.object(lifecycle, "SDME_STATE", state), \
                patch.object(lifecycle, "stop_builder") as stop, \
                patch.object(lifecycle, "control") as control:
            lifecycle.reset_builder(target, "paur12345678")
        stop.assert_called_once_with("paur12345678")
        self.assertEqual([call.args for call in control.call_args_list], [
            ("rm", "--force", "paur12345678"),
            ("fs", "rm", "--force", "paur12345678-base"),
        ])
        self.assertFalse(target.exists())

    def run_broker(self, status=0, disconnect=False, start_failure=False):
        peer = Mock()
        peer.getsockopt.return_value = struct.pack("3i", 123, 1000, 1000)
        peer.makefile.return_value = io.BytesIO(b'{"package":"example","clean":true,"reset":false}\n')
        target = self.scratch / "1000-x86_64"
        target.mkdir()
        data = {"schema": 2, "architecture": "x86_64", "template": "digest"}
        process = Mock(returncode=status)
        process.poll.side_effect = [None, status, status] if not disconnect else [None, None]
        poller = Mock()
        poller.poll.return_value = [(123, 1)] if disconnect else []
        with patch.object(lifecycle, "STATE", self.scratch), \
                patch.object(lifecycle, "lock", side_effect=lambda path: contextlib.nullcontext()), \
                patch.object(lifecycle, "trusted_directory"), \
                patch.object(lifecycle.pwd, "getpwuid"), \
                patch.object(lifecycle.platform, "machine", return_value="x86_64"), \
                patch.object(lifecycle, "preflight"), \
                patch.object(lifecycle, "template_digest", return_value="digest"), \
                patch.object(lifecycle, "metadata", return_value=data), \
                patch.object(lifecycle, "stop_builder") as stop, \
                patch.object(lifecycle, "control",
                             side_effect=subprocess.CalledProcessError(1, ["sdme"])
                             if start_failure else None), \
                patch.object(lifecycle.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(lifecycle.select, "poll", return_value=poller):
            if disconnect:
                with self.assertRaisesRegex(ValueError, "disconnected"):
                    lifecycle.serve(peer)
            elif start_failure:
                with self.assertRaises(subprocess.CalledProcessError):
                    lifecycle.serve(peer)
            else:
                self.assertEqual(lifecycle.serve(peer), status)
        self.assertEqual(stop.call_count, 2)
        if not start_failure:
            self.assertIs(spawn.call_args.kwargs["stdout"], peer)
            self.assertIs(spawn.call_args.kwargs["stdin"], peer)
            self.assertIs(spawn.call_args.kwargs["stderr"], lifecycle.sys.stderr)
            self.assertIn("--clean", spawn.call_args.args[0])
        if disconnect:
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=15)
        if start_failure:
            spawn.assert_not_called()

    def test_broker_preserves_sdme_exec_return_codes(self):
        for status in (0, 42):
            with self.subTest(status=status):
                self.run_broker(status)
                shutil.rmtree(self.scratch / "1000-x86_64")

    def test_broker_disconnect_stops_guest(self):
        self.run_broker(disconnect=True)

    def test_broker_start_failure_stops_guest(self):
        self.run_broker(start_failure=True)

    def test_sdme_version_is_pinned(self):
        with patch.object(client.subprocess, "run", return_value=Mock(stdout="sdme 0.20.0\n")), \
                self.assertRaisesRegex(ValueError, "sdme 0.21.0"):
            client.check_sdme_version()

    def test_guest_uses_booted_networking_not_manual_dhcpcd(self):
        with patch.object(guest.sys, "argv", ["guest.py", "example"]), \
                patch.object(guest.os, "getuid", return_value=0), \
                patch.object(guest, "rpc_package", side_effect=ValueError("offline")), \
                patch.object(guest, "run") as command, self.assertRaisesRegex(ValueError, "offline"):
            guest.main()
        self.assertEqual(command.call_args_list[0].args[0], [
            "/usr/lib/systemd/systemd-networkd-wait-online", "--interface=host0", "--ipv4", "--timeout=60",
        ])
        self.assertEqual(command.call_args_list[1].args[0],
                         ["/usr/bin/pacman", "-Syu", "--noconfirm"])

    def test_sdme_dependency_is_prepared_as_native_package(self):
        repository = PROFILE.parents[1]
        task = tomllib.loads((repository / "mise.toml").read_text())["tasks"]["prepare"]
        content = task["run"]
        self.assertEqual(task["dir"], "{{config_root}}")
        self.assertFalse((PROFILE / "mkosi.postinst").exists())
        self.assertIn("releases/download/v0.21.0/sdme-0.21.0-1-x86_64.pkg.tar.zst", content)
        self.assertIn("d8b844cabf8a659b2061745e2ae1c6509308ab1a2f9405fc1c8e850ffddd0455", content)
        self.assertIn("sha256sum --check --status", content)
        self.assertIn('mv "$download" "$package"', content)
        self.assertIn("mkosi.packages/", content)
        self.assertIn("        sdme\n", (PROFILE / "mkosi.conf").read_text())
        self.assertNotIn("curl", (CODE / "lifecycle.py").read_text())

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

    def test_reset_preflight_requires_only_run0_and_pinned_sdme(self):
        with patch.object(client.Path, "is_file", return_value=True), \
                patch.object(client.platform, "machine", return_value="x86_64"), \
                patch.object(client.sys, "version_info", (3, 10)), \
                patch.object(client.Path, "exists", side_effect=AssertionError("IPE must not be checked")), \
                patch.object(client.subprocess, "run", return_value=Mock(stdout="sdme 0.21.0\n")) as command:
            client.preflight(reset=True)
        self.assertEqual(command.call_args.args[0], ["/usr/bin/sdme", "--version"])

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
        versions = [Mock(stdout="systemd 257\n"), Mock(stdout="mkosi 26\n"),
                    Mock(stdout="systemd 257\n"), Mock(stdout="sdme 0.21.0\n")]
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
