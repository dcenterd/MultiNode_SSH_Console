import os
import socket
import tempfile
import sys
import threading
import unittest

import paramiko

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
from core import Target  # noqa: E402


class ParseTargets(unittest.TestCase):
    def test_mixed_separators_and_dedupe(self):
        t, e = core.parse_targets("10.0.0.1, 10.0.0.2;10.0.0.3\n10.0.0.1\n\n  router1  ")
        self.assertEqual([x.host for x in t], ["10.0.0.1", "10.0.0.2", "10.0.0.3", "router1"])
        self.assertEqual(e, [])

    def test_ports_comments_ipv6(self):
        t, e = core.parse_targets("10.0.0.1:2222  # lab\n# whole line\n[::1]:2200\nfe80::1\n", 22)
        self.assertEqual(
            t, [Target("10.0.0.1", 2222), Target("::1", 2200), Target("fe80::1", 22)]
        )
        self.assertEqual(e, [])

    def test_default_port_and_bom(self):
        t, _ = core.parse_targets("﻿192.168.1.1\n", 830)
        self.assertEqual(t, [Target("192.168.1.1", 830)])

    def test_invalid_entries_reported(self):
        t, e = core.parse_targets("good.example\nbad_host!\n1.2.3.4:99999\n1.2.3.4:abc\n[::1\n")
        self.assertEqual([x.host for x in t], ["good.example"])
        bad = [tok for tok, _ in e]
        self.assertIn("bad_host!", bad)
        self.assertIn("1.2.3.4:99999", bad)
        self.assertIn("1.2.3.4:abc", bad)
        self.assertIn("[::1", bad)


class FakeServer(paramiko.ServerInterface):
    auth_attempts = 0  # how many times any client sent a password
    exec_count = 0  # how many commands were actually executed

    def check_auth_password(self, username, password):
        FakeServer.auth_attempts += 1
        ok = username == "admin" and password == "secret"
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, channel, term, width, height, pw, ph, modes):
        return True

    def check_channel_shell_request(self, channel):
        def shell():
            channel.send("router> ")
            buf = ""
            while True:
                try:
                    data = channel.recv(1024)
                except Exception:
                    return
                if not data:
                    return
                buf += data.decode()
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line == "exit":
                        channel.close()
                        return
                    channel.send(f"{line}\r\nresult-of-{line}\r\nrouter> ")

        threading.Thread(target=shell, daemon=True).start()
        return True

    def check_channel_exec_request(self, channel, command):
        FakeServer.exec_count += 1
        cmd = command.decode()

        def respond():
            threading.Event().wait(0.1)  # let the exec request be acknowledged first
            if cmd.startswith("echo "):
                channel.send(cmd[5:] + "\n")
                channel.send_exit_status(0)
            elif cmd == "fail":
                channel.send_stderr("boom\n")
                channel.send_exit_status(3)
            elif cmd == "hang":
                threading.Event().wait(30)
            channel.close()

        threading.Thread(target=respond, daemon=True).start()
        return True


def start_server(key=None):
    key = key or paramiko.RSAKey.generate(2048)
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(20)

    def serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            t = paramiko.Transport(conn)
            t.add_server_key(key)
            try:
                t.start_server(server=FakeServer())
            except Exception:
                pass

    threading.Thread(target=serve, daemon=True).start()
    return sock


class RunCommand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sock = start_server()
        cls.target = Target("127.0.0.1", cls.sock.getsockname()[1])

    @classmethod
    def tearDownClass(cls):
        cls.sock.close()

    def run_cmd(self, cmd, password="secret", **kw):
        return core.run_command(self.target, "admin", password, cmd, connect_timeout=5, **kw)

    def test_success_output(self):
        r = self.run_cmd("echo hello world")
        self.assertEqual((r.status, r.exit_status), (core.OK, 0))
        self.assertEqual(r.output.strip(), "hello world")

    def test_nonzero_exit_and_stderr(self):
        r = self.run_cmd("fail")
        self.assertEqual((r.status, r.exit_status), (core.FAILED, 3))
        self.assertIn("boom", r.error)

    def test_bad_password(self):
        r = self.run_cmd("echo x", password="nope")
        self.assertEqual(r.status, core.ERROR)
        self.assertIn("Authentication failed", r.error)

    def test_command_timeout(self):
        r = self.run_cmd("hang", command_timeout=1)
        self.assertEqual(r.status, core.TIMEOUT)

    def test_connection_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        r = core.run_command(Target("127.0.0.1", port), "admin", "secret", "echo x", connect_timeout=2)
        # "refused" on Linux; Windows retries and reports a connect timeout instead
        self.assertIn(r.status, (core.ERROR, core.TIMEOUT))

    def test_run_all_parallel_and_order(self):
        good = self.target
        dead = Target("127.0.0.1", 1)
        seen = []
        results = core.run_all(
            [good, dead, good.__class__(good.host, good.port)], "admin", "secret", "echo hi",
            connect_timeout=2, workers=3, on_result=seen.append,
        )
        statuses = [r.status for r in results]
        self.assertEqual(statuses[0], core.OK)
        self.assertEqual(statuses[2], core.OK)
        # a dead port is "refused" on Linux but a connect timeout on Windows
        self.assertIn(statuses[1], (core.ERROR, core.TIMEOUT))
        self.assertEqual(len(seen), 3)

    def test_cancel_before_start(self):
        ev = threading.Event()
        ev.set()
        r = self.run_cmd("echo x", cancel=ev)
        self.assertEqual(r.status, core.CANCELLED)


class Presets(unittest.TestCase):
    def test_disruptive_commands_detected(self):
        cmds = ["olStatus all", "olOff 3", "OLREBOOT all", "reboot", "about", "olOn 1-4"]
        self.assertEqual(
            core.disruptive_commands(cmds), ["olOff 3", "OLREBOOT all", "reboot", "olOn 1-4"]
        )

    def test_every_disruptive_preset_is_flagged_and_reads_are_not(self):
        for label, template in core.APC_PRESETS:
            flagged = bool(core.disruptive_commands([template.replace("<n>", "1")]))
            self.assertEqual(flagged, label.startswith(("Outlet O", "Outlet REBOOT", "Reboot")), label)


class MultipleCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sock = start_server()
        cls.target = Target("127.0.0.1", cls.sock.getsockname()[1])

    @classmethod
    def tearDownClass(cls):
        cls.sock.close()

    def run_cmds(self, text, **kw):
        return core.run_command(self.target, "admin", "secret", text, connect_timeout=5, **kw)

    def test_split_commands(self):
        self.assertEqual(
            core.split_commands("  one \n\n# note\n two\n"), ["one", "two"]
        )
        self.assertEqual(core.split_commands("a\nb", separate=False), ["a\nb"])
        self.assertEqual(core.split_commands("  \n# only comment\n"), [])

    def test_several_commands_each_with_own_header(self):
        r = self.run_cmds("echo one\n# skipped comment\n\necho two")
        self.assertEqual((r.status, r.exit_status), (core.OK, 0))
        self.assertEqual(r.output.split("\n\n"), ["$ echo one\none", "$ echo two\ntwo"])

    def test_single_command_output_is_not_decorated(self):
        r = self.run_cmds("echo only")
        self.assertEqual(r.output, "only")

    def test_stops_after_first_failure(self):
        before = FakeServer.exec_count
        r = self.run_cmds("echo a\nfail\necho c")
        self.assertEqual((r.status, r.exit_status), (core.FAILED, 3))
        self.assertIn("$ fail   [exit 3]", r.output)
        self.assertIn("$ echo c\n(skipped - an earlier command failed)", r.output)
        self.assertIn("boom", r.error)
        self.assertEqual(FakeServer.exec_count - before, 2)  # "echo c" never ran

    def test_continue_after_failure_when_asked(self):
        before = FakeServer.exec_count
        r = self.run_cmds("echo a\nfail\necho c", stop_on_error=False)
        self.assertEqual((r.status, r.exit_status), (core.FAILED, 3))
        self.assertIn("$ echo c\nc", r.output)
        self.assertEqual(FakeServer.exec_count - before, 3)

    def test_timeout_is_per_command_and_skips_the_rest(self):
        r = self.run_cmds("echo a\nhang\necho c", command_timeout=1)
        self.assertEqual(r.status, core.TIMEOUT)
        self.assertIn("$ echo a\na", r.output)
        self.assertIn("(timed out)", r.output)
        self.assertIn("(skipped", r.output)

    def test_send_as_one_script(self):
        before = FakeServer.exec_count
        r = self.run_cmds("echo a\necho b", separate_commands=False)
        self.assertEqual(FakeServer.exec_count - before, 1)
        self.assertIn("a", r.output)
        self.assertIn("echo b", r.output)  # the fake server just echoes the text

    def test_shell_mode_types_all_commands_into_one_session(self):
        r = self.run_cmds("show version\nshow clock", use_shell=True, command_timeout=10)
        self.assertEqual(r.status, core.OK)
        self.assertLess(r.output.index("result-of-show version"), r.output.index("result-of-show clock"))

    def test_dry_run_counts_commands(self):
        r = self.run_cmds("echo a\necho b\necho c", dry_run=True)
        self.assertIn("3 commands were NOT executed", r.output)

    def test_empty_command_is_an_error(self):
        r = self.run_cmds("\n# nothing\n")
        self.assertEqual(r.status, core.ERROR)


class PrecheckAndDryRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sock = start_server()
        cls.target = Target("127.0.0.1", cls.sock.getsockname()[1])

    @classmethod
    def tearDownClass(cls):
        cls.sock.close()

    def dead_target(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return Target("127.0.0.1", port)

    def test_port_check_ok_and_failure(self):
        self.assertIsNone(core.port_check(self.target, 2))
        reason = core.port_check(self.dead_target(), 2)
        self.assertIsNotNone(reason)
        self.assertIn("port", reason.lower())

    def test_precheck_marks_dead_host_unreachable_fast(self):
        r = core.run_command(
            self.dead_target(), "admin", "secret", "echo x", connect_timeout=30, precheck_timeout=2
        )
        self.assertEqual(r.status, core.UNREACHABLE)
        self.assertLess(r.seconds, 5)
        self.assertTrue(r.error)

    def test_precheck_passes_through_to_ssh(self):
        r = core.run_command(
            self.target, "admin", "secret", "echo hi", connect_timeout=5, precheck_timeout=2
        )
        self.assertEqual((r.status, r.output.strip()), (core.OK, "hi"))

    def test_dry_run_logs_in_but_runs_nothing(self):
        before = FakeServer.exec_count
        r = core.run_command(
            self.target, "admin", "secret", "echo should-not-run", connect_timeout=5, dry_run=True
        )
        self.assertEqual(r.status, core.OK)
        self.assertIsNone(r.exit_status)
        self.assertIn("dry run", r.output)
        self.assertEqual(FakeServer.exec_count, before)

    def test_dry_run_reports_bad_password(self):
        r = core.run_command(
            self.target, "admin", "wrong", "echo x", connect_timeout=5, dry_run=True
        )
        self.assertEqual(r.status, core.ERROR)
        self.assertIn("Authentication failed", r.error)

    def test_dry_run_still_asks_for_host_key(self):
        tmp = tempfile.TemporaryDirectory()
        asked = []
        v = core.HostKeyVerifier(
            os.path.join(tmp.name, "kh"), lambda q: (asked.append(q), core.ACCEPT)[1]
        )
        r = core.run_command(
            self.target, "admin", "secret", "echo x", connect_timeout=5, verifier=v, dry_run=True
        )
        self.assertEqual(r.status, core.OK)
        self.assertEqual(len(asked), 1)  # key accepted during the dry run...
        v2 = core.HostKeyVerifier(os.path.join(tmp.name, "kh"), lambda q: core.REJECT)
        r2 = core.run_command(self.target, "admin", "secret", "echo x", connect_timeout=5, verifier=v2)
        self.assertEqual(r2.status, core.OK)  # ...so the real run is no longer prompted
        tmp.cleanup()


class KeyOnNewAddress(unittest.TestCase):
    """Same device (same key) reachable at a different address."""

    def setUp(self):
        self.key = paramiko.RSAKey.generate(2048)
        self.old_sock = start_server(self.key)
        self.new_sock = start_server(self.key)
        self.old = Target("127.0.0.1", self.old_sock.getsockname()[1])
        self.new = Target("127.0.0.1", self.new_sock.getsockname()[1])
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "known_hosts")
        self.questions = []
        self.answer = core.ACCEPT

    def tearDown(self):
        self.old_sock.close()
        self.new_sock.close()
        self.tmp.cleanup()

    def prompt(self, q):
        self.questions.append(q)
        return self.answer

    def run_on(self, verifier, target):
        return core.run_command(target, "admin", "secret", "echo ok", connect_timeout=5, verifier=verifier)

    def trust_old(self):
        v = core.HostKeyVerifier(self.path, lambda q: core.ACCEPT)
        self.assertEqual(self.run_on(v, self.old).status, core.OK)

    def test_prompt_mentions_old_address(self):
        self.trust_old()
        v = core.HostKeyVerifier(self.path, self.prompt)
        self.assertEqual(self.run_on(v, self.new).status, core.OK)
        q = self.questions[0]
        self.assertFalse(q.changed)
        self.assertEqual(q.same_key_hosts, (f"[127.0.0.1]:{self.old.port}",))
        text = open(self.path).read()  # accepting adds, keeps the old address
        self.assertIn(f"[127.0.0.1]:{self.old.port}", text)
        self.assertIn(f"[127.0.0.1]:{self.new.port}", text)

    def test_accept_and_forget_old_address(self):
        self.trust_old()
        self.answer = core.ACCEPT_MOVE
        v = core.HostKeyVerifier(self.path, self.prompt)
        self.assertEqual(self.run_on(v, self.new).status, core.OK)
        text = open(self.path).read()
        self.assertNotIn(f"[127.0.0.1]:{self.old.port}", text)
        self.assertIn(f"[127.0.0.1]:{self.new.port}", text)

    def test_auto_trust_known_key_skips_prompt(self):
        self.trust_old()
        v = core.HostKeyVerifier(self.path, self.prompt, auto_trust_known_keys=True)
        self.assertEqual(self.run_on(v, self.new).status, core.OK)
        self.assertEqual(self.questions, [])

    def test_auto_trust_does_not_apply_to_unknown_keys(self):
        v = core.HostKeyVerifier(self.path, self.prompt, auto_trust_known_keys=True)
        self.assertEqual(self.run_on(v, self.new).status, core.OK)
        self.assertEqual(len(self.questions), 1)  # nothing known yet -> still asked
        self.assertEqual(self.questions[0].same_key_hosts, ())


class HostKeys(unittest.TestCase):
    def setUp(self):
        self.sock = start_server()
        self.target = Target("127.0.0.1", self.sock.getsockname()[1])
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "known_hosts")
        self.questions = []
        self.answer = core.ACCEPT
        FakeServer.auth_attempts = 0

    def tearDown(self):
        self.sock.close()
        self.tmp.cleanup()

    def prompt(self, q):
        self.questions.append(q)
        return self.answer

    def run_cmd(self, verifier, target=None):
        return core.run_command(
            target or self.target, "admin", "secret", "echo ok", connect_timeout=5, verifier=verifier
        )

    def test_first_use_accept_is_remembered(self):
        v = core.HostKeyVerifier(self.path, self.prompt)
        self.assertEqual(self.run_cmd(v).status, core.OK)
        self.assertEqual(len(self.questions), 1)
        q = self.questions[0]
        self.assertFalse(q.changed)
        self.assertTrue(q.fingerprint.startswith("SHA256:"))
        self.assertTrue(q.public_key.startswith("ssh-rsa "))
        # persisted to disk, and a fresh verifier trusts it without asking
        self.assertIn(f"[127.0.0.1]:{self.target.port}", open(self.path).read())
        v2 = core.HostKeyVerifier(self.path, self.prompt)
        self.assertEqual(self.run_cmd(v2).status, core.OK)
        self.assertEqual(len(self.questions), 1)

    def test_first_use_reject_never_sends_password(self):
        self.answer = core.REJECT
        v = core.HostKeyVerifier(self.path, self.prompt)
        r = self.run_cmd(v)
        self.assertEqual(r.status, core.ERROR)
        self.assertIn("not accepted", r.error)
        self.assertEqual(FakeServer.auth_attempts, 0)
        self.assertFalse(os.path.exists(self.path))

    def test_changed_key_warns_and_can_be_replaced(self):
        os.makedirs(os.path.dirname(self.path))
        other = paramiko.RSAKey.generate(2048)
        hk = paramiko.HostKeys()
        hk.add(f"[127.0.0.1]:{self.target.port}", "ssh-rsa", other)
        hk.save(self.path)

        self.answer = core.REJECT
        v = core.HostKeyVerifier(self.path, self.prompt)
        r = self.run_cmd(v)
        self.assertEqual(r.status, core.ERROR)
        self.assertIn("HOST KEY CHANGED", r.error)
        self.assertTrue(self.questions[0].changed)
        self.assertEqual(self.questions[0].old_fingerprint, core.fingerprint(other))
        self.assertEqual(FakeServer.auth_attempts, 0)

        self.answer = core.ACCEPT
        self.assertEqual(self.run_cmd(v).status, core.OK)
        asked = len(self.questions)
        v2 = core.HostKeyVerifier(self.path, self.prompt)
        self.assertEqual(self.run_cmd(v2).status, core.OK)
        self.assertEqual(len(self.questions), asked)  # replaced key is now trusted

    def test_accept_all_new_hosts(self):
        other_sock = start_server()
        try:
            other = Target("127.0.0.1", other_sock.getsockname()[1])
            self.answer = core.ACCEPT_ALL
            v = core.HostKeyVerifier(self.path, self.prompt)
            results = [self.run_cmd(v), self.run_cmd(v, other)]
            self.assertEqual([r.status for r in results], [core.OK, core.OK])
            self.assertEqual(len(self.questions), 1)  # asked once, second host auto-accepted
        finally:
            other_sock.close()

    def test_accept_all_does_not_cover_changed_keys(self):
        v = core.HostKeyVerifier(self.path, self.prompt)
        v.accept_all_new = True
        os.makedirs(os.path.dirname(self.path))
        hk = paramiko.HostKeys()
        hk.add(f"[127.0.0.1]:{self.target.port}", "ssh-rsa", paramiko.RSAKey.generate(2048))
        hk.save(self.path)
        v = core.HostKeyVerifier(self.path, self.prompt)
        v.accept_all_new = True
        self.answer = core.REJECT
        r = self.run_cmd(v)
        self.assertEqual(r.status, core.ERROR)
        self.assertEqual(len(self.questions), 1)  # still prompted


if __name__ == "__main__":
    unittest.main()
