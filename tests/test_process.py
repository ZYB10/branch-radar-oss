import sys
import threading
import time
import unittest

from branch_radar.process import CommandRunner


class BoundedProcessTests(unittest.TestCase):
    def test_cancel_event_stops_a_running_process_promptly(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.05, cancel_event.set)
        timer.daemon = True
        started = time.monotonic()
        timer.start()
        try:
            result = CommandRunner().run_limited(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdout_limit_bytes=1024,
                timeout=30,
                cancel_event=cancel_event,
            )
        finally:
            timer.cancel()
            timer.join(timeout=1)

        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.returncode, 130)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.ok)

    def test_stdout_is_capped_and_noisy_child_is_stopped(self):
        result = CommandRunner().run_limited(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 1000000); sys.stdout.flush()",
            ],
            stdout_limit_bytes=4096,
            timeout=10,
        )
        self.assertTrue(result.stdout_truncated)
        self.assertEqual(len(result.stdout.encode("utf-8")), 4096)
        self.assertFalse(result.timed_out)

    def test_stderr_is_drained_with_its_own_cap(self):
        result = CommandRunner().run_limited(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.buffer.write(b'e' * 100000); sys.stderr.flush()",
            ],
            stdout_limit_bytes=1024,
            stderr_limit_bytes=2048,
            timeout=10,
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.stderr_truncated)
        self.assertEqual(len(result.stderr.encode("utf-8")), 2048)

    def test_progress_receives_incremental_output_without_affecting_result(self):
        updates = []
        result = CommandRunner().run_limited(
            [
                sys.executable,
                "-c",
                "import sys; print('out'); print('err', file=sys.stderr)",
            ],
            stdout_limit_bytes=1024,
            timeout=10,
            progress=lambda stream, chunk: updates.append((stream, chunk)),
        )
        self.assertTrue(result.ok)
        self.assertTrue(any(
            stream == "stdout" and "out" in chunk for stream, chunk in updates
        ))
        self.assertTrue(any(
            stream == "stderr" and "err" in chunk for stream, chunk in updates
        ))

    def test_small_progress_is_reported_while_child_is_still_running(self):
        progress_seen = threading.Event()
        completed = threading.Event()
        result = []

        def run():
            result.append(CommandRunner().run_limited(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys,time; "
                        "sys.stderr.write('Receiving objects: 10% (1/10)\\r'); "
                        "sys.stderr.flush(); time.sleep(1)"
                    ),
                ],
                stdout_limit_bytes=1024,
                timeout=5,
                progress=lambda _stream, _chunk: progress_seen.set(),
            ))
            completed.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.assertTrue(progress_seen.wait(timeout=0.5))
        self.assertFalse(completed.is_set())
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result[0].ok)


if __name__ == "__main__":
    unittest.main()
