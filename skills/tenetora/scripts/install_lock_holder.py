#!/usr/bin/env python3
"""Hold the shared machine-install lock for an outer shell installer."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CLI_DIR = SCRIPT_DIR.parent / "cli"
if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from tenetora.install_lock import install_lock, install_lock_path, new_install_lock_token  # noqa: E402


def write_atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="ascii", newline="") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start_lock_proof_server(owner_value: str) -> tuple[object, threading.Event, threading.Thread, str]:
    """Expose a live, token-authenticated proof while the file lock is held."""

    if os.name != "nt":
        raise RuntimeError("Windows lock proof channels are only supported on Windows")
    from multiprocessing.connection import AuthenticationError, Listener

    endpoint = rf"\\.\pipe\tenetora-install-{uuid.uuid4().hex}"
    listener = Listener(endpoint, family="AF_PIPE", authkey=owner_value.encode("ascii"))
    stop_event = threading.Event()

    def serve() -> None:
        while not stop_event.is_set():
            try:
                connection = listener.accept()
            except AuthenticationError:
                continue
            except (OSError, EOFError):
                break

            def handle_connection(client: object) -> None:
                try:
                    if client.poll(2.0):
                        request = client.recv()
                        client.send("held" if request == "check" and not stop_event.is_set() else "not-held")
                except (EOFError, OSError):
                    pass
                finally:
                    client.close()

            threading.Thread(
                target=handle_connection,
                args=(connection,),
                name="tenetora-install-lock-proof-client",
                daemon=True,
            ).start()

    thread = threading.Thread(target=serve, name="tenetora-install-lock-proof", daemon=True)
    thread.start()
    return listener, stop_event, thread, endpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--help", action="help")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--proof-file", type=Path)
    parser.add_argument("--print-path", action="store_true")
    args = parser.parse_args(argv)
    if args.print_path:
        print(install_lock_path(args.home))
        return 0
    if args.ready_file is None:
        parser.error("--ready-file is required while holding the lock")
    owner_value = new_install_lock_token()
    with install_lock(args.home, owner_value):
        proof_listener = None
        proof_stop = None
        proof_thread = None
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            if args.proof_file is not None:
                proof_listener, proof_stop, proof_thread, endpoint = start_lock_proof_server(owner_value)
                write_atomic_text(args.proof_file, f"{endpoint}\n")
            write_atomic_text(args.ready_file, f"{owner_value}\n")
            sys.stdin.buffer.read()
        finally:
            if proof_stop is not None:
                proof_stop.set()
            if proof_listener is not None:
                proof_listener.close()
            if proof_thread is not None:
                proof_thread.join(timeout=1)
            if args.proof_file is not None:
                args.proof_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
