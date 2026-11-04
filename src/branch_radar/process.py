from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


@dataclass
class ProcessResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.returncode == 0
            and not self.timed_out
            and not self.stdout_truncated
            and not self.cancelled
        )


class CommandRunner:
    """Small shell-free subprocess boundary that is easy to replace in tests."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = 30,
        env: Mapping[str, str] | None = None,
        replace_env: bool = False,
    ) -> ProcessResult:
        started = time.monotonic()
        process_env: dict[str, str] = {} if replace_env else dict(os.environ)
        if env:
            process_env.update(env)
        process = subprocess.Popen(
                list(argv),
                cwd=str(cwd) if cwd else None,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=process_env,
                shell=False,
                start_new_session=os.name == "posix",
            )
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=timeout)
            return ProcessResult(
                argv=list(argv),
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
                stdout, stderr = process.communicate()
            return ProcessResult(
                argv=list(argv),
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )

    def run_limited(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = 30,
        env: Mapping[str, str] | None = None,
        replace_env: bool = False,
        cancel_event: threading.Event | None = None,
        progress: Callable[[str, str], None] | None = None,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int = 65_536,
    ) -> ProcessResult:
        """Run a command while bounding captured output in memory.

        The child is stopped as soon as stdout exceeds its budget. Reader threads
        continue draining both pipes, so a noisy stderr cannot deadlock the child.
        When a cancellation event is supplied, it is observed within 100ms.
        """
        started = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            return ProcessResult(
                argv=list(argv),
                returncode=130,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - started,
                cancelled=True,
            )
        process_env: dict[str, str] = {} if replace_env else dict(os.environ)
        if env:
            process_env.update(env)
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_env,
            shell=False,
            start_new_session=os.name == "posix",
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_overflow = threading.Event()
        stderr_overflow = threading.Event()
        stdout_limit = max(1, int(stdout_limit_bytes))
        stderr_limit = max(1, int(stderr_limit_bytes))

        wake = threading.Event()

        stdout_thread = threading.Thread(
            target=_drain_limited,
            args=(
                process.stdout, stdout_buffer, stdout_limit, stdout_overflow,
                wake, "stdout", progress,
            ),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_limited,
            args=(
                process.stderr, stderr_buffer, stderr_limit, stderr_overflow,
                wake, "stderr", progress,
            ),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        exit_thread = threading.Thread(
            target=_wake_on_exit, args=(process, wake), daemon=True
        )
        exit_thread.start()
        stdin_thread = None
        if stdin is not None:
            stdin_thread = threading.Thread(
                target=_write_stdin,
                args=(process.stdin, stdin.encode("utf-8")),
                daemon=True,
            )
            stdin_thread.start()

        timed_out = False
        cancelled = False
        deadline = started + timeout
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate_process_tree(process)
                break
            if stdout_overflow.is_set():
                _terminate_process_tree(process)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_process_tree(process)
                break
            # Woken by child exit or by an stdout budget overflow; a spurious
            # wake just re-evaluates the same conditions with less time left.
            wake.wait(min(remaining, 0.1) if cancel_event is not None else remaining)
            wake.clear()

        try:
            process.wait(timeout=0.5 if cancelled else 5)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        exit_thread.join(timeout=5)
        if stdin_thread is not None:
            stdin_thread.join(timeout=5)
        return ProcessResult(
            argv=list(argv),
            returncode=(
                130
                if cancelled
                else 124
                if timed_out
                else int(process.returncode)
            ),
            stdout=bytes(stdout_buffer).decode("utf-8", "replace"),
            stderr=bytes(stderr_buffer).decode("utf-8", "replace"),
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
            stdout_truncated=stdout_overflow.is_set(),
            stderr_truncated=stderr_overflow.is_set(),
            cancelled=cancelled,
        )


def _drain_limited(
    stream: BinaryIO,
    buffer: bytearray,
    limit: int,
    overflow: threading.Event,
    wake: threading.Event,
    stream_name: str,
    progress: Callable[[str, str], None] | None,
) -> None:
    try:
        while True:
            # BufferedReader.read(size) may wait for ``size`` bytes or EOF,
            # which hides small progress writes until the child exits. read1
            # performs at most one raw pipe read and returns available bytes.
            read1 = getattr(stream, "read1", None)
            chunk = (
                read1(65_536)
                if callable(read1)
                else os.read(stream.fileno(), 65_536)
            )
            if not chunk:
                return
            if progress is not None:
                try:
                    progress(stream_name, chunk.decode("utf-8", "replace"))
                except Exception:
                    # Progress is advisory and must never strand a pipe reader.
                    pass
            remaining = limit - len(buffer)
            if remaining > 0:
                buffer.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                overflow.set()
                wake.set()
    finally:
        stream.close()


def _wake_on_exit(
    process: subprocess.Popen[bytes], wake: threading.Event
) -> None:
    """Turn child exit into an event so the supervisor never polls."""
    try:
        process.wait()
    except Exception:  # pragma: no cover - wait() only raises on caller misuse.
        pass
    finally:
        wake.set()


def _write_stdin(stream: BinaryIO, value: bytes) -> None:
    try:
        stream.write(value)
        stream.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except PermissionError:
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
    except ProcessLookupError:
        pass


def _kill_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except PermissionError:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    except ProcessLookupError:
        pass
