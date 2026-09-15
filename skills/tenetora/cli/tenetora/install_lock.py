"""Cross-platform machine-install lock shared by every installer entrypoint."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import threading
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from .file_lock import locked_file
from .path_security import ensure_unredirected_directory, validate_unredirected_path


def install_lock_path(home: Path) -> Path:
    root = validate_unredirected_path(home, label="Tenetora install home")
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / f"tenetora-install-{digest}.lock"


TOKEN_ENV = "TENETORA_OUTER_INSTALL_LOCK_TOKEN"
TOKEN_PATTERN_LENGTH = 64
LOCK_FDS_ENV = "TENETORA_OUTER_INSTALL_LOCK_FDS"
LOCK_HANDLES_ENV = "TENETORA_OUTER_INSTALL_LOCK_HANDLES"
LOCK_PROOFS_ENV = "TENETORA_OUTER_INSTALL_LOCK_PROOFS"


def new_install_lock_token() -> str:
    return secrets.token_hex(TOKEN_PATTERN_LENGTH // 2)


@contextmanager
def install_lock(home: Path, owner_token: str = "") -> Iterator[object]:
    with locked_file(install_lock_path(home)) as handle:
        handle.seek(0)
        handle.truncate()
        handle.write((owner_token + "\n").encode("ascii"))
        handle.flush()
        try:
            yield handle
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()


def _descriptor_map(environment_name: str) -> dict[str, int]:
    try:
        payload = json.loads(os.environ.get(environment_name, "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


def _string_map(environment_name: str) -> dict[str, str]:
    try:
        payload = json.loads(os.environ.get(environment_name, "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }


def _inherited_lock_descriptor(home: Path) -> tuple[int | None, int | None]:
    key = str(home)
    return _descriptor_map(LOCK_FDS_ENV).get(key), _descriptor_map(LOCK_HANDLES_ENV).get(key)


def _inherited_lock_proof(home: Path) -> str | None:
    return _string_map(LOCK_PROOFS_ENV).get(str(home))


def install_lock_subprocess_kwargs() -> dict[str, object]:
    """Return subprocess options that preserve a proven outer lock."""

    descriptors = _descriptor_map(LOCK_FDS_ENV)
    handles = _descriptor_map(LOCK_HANDLES_ENV)
    if os.name == "nt":
        native_handles = tuple(handles.values())
        if not native_handles:
            return {}
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": list(native_handles)}
        return {"close_fds": True, "startupinfo": startupinfo}
    file_descriptors = tuple(descriptors.values())
    return {"pass_fds": file_descriptors} if file_descriptors else {}


def _duplicate_windows_handle(native_handle: int) -> int:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.DuplicateHandle.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    kernel32.DuplicateHandle.restype = ctypes.c_int
    duplicated = ctypes.c_void_p()
    current_process = kernel32.GetCurrentProcess()
    if not kernel32.DuplicateHandle(
        current_process,
        ctypes.c_void_p(native_handle),
        current_process,
        ctypes.byref(duplicated),
        0,
        False,
        0x00000002,
    ) or not duplicated.value:
        error = ctypes.get_last_error()
        raise OSError(error, "DuplicateHandle failed")
    return int(duplicated.value)


def _close_windows_handle(native_handle: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(ctypes.c_void_p(native_handle))


def _query_lock_proof(endpoint: str, owner_token: str) -> bool:
    """Verify a Windows outer lock through its live local proof channel."""

    try:
        from multiprocessing.connection import AuthenticationError, Client

        connection = Client(endpoint, family="AF_PIPE", authkey=owner_token.encode("ascii"))
        try:
            connection.send("check")
            return connection.recv() == "held"
        finally:
            connection.close()
    except (AuthenticationError, EOFError, ImportError, OSError, TypeError, ValueError):
        return False


def _start_windows_lock_proof_server(
    owner_token: str,
) -> tuple[object, threading.Event, threading.Thread, str]:
    """Expose a live, token-authenticated proof for a Python-held lock."""

    if os.name != "nt":
        raise RuntimeError("Windows lock proof channels are only supported on Windows")
    from multiprocessing.connection import AuthenticationError, Listener

    endpoint = rf"\\.\pipe\tenetora-install-{uuid.uuid4().hex}"
    listener = Listener(endpoint, family="AF_PIPE", authkey=owner_token.encode("ascii"))
    stopped = threading.Event()

    def serve() -> None:
        while not stopped.is_set():
            try:
                client = listener.accept()
            except AuthenticationError:
                continue
            except (EOFError, OSError):
                break

            def respond(connection: object) -> None:
                try:
                    if connection.poll(2.0):
                        request = connection.recv()
                        connection.send("held" if request == "check" and not stopped.is_set() else "not-held")
                except (EOFError, OSError):
                    pass
                finally:
                    connection.close()

            threading.Thread(
                target=respond,
                args=(client,),
                name="tenetora-install-lock-proof-client",
                daemon=True,
            ).start()

    thread = threading.Thread(target=serve, name="tenetora-install-lock-proof", daemon=True)
    thread.start()
    return listener, stopped, thread, endpoint


def _descriptor_matches_lock(path: Path, descriptor: int | None, native_handle: int | None) -> bool:
    try:
        path_stat = path.stat()
        if os.name == "nt":
            if native_handle is None:
                return False
            duplicated_handle = _duplicate_windows_handle(native_handle)
            inherited_fd: int | None = None
            try:
                import msvcrt

                inherited_fd = msvcrt.open_osfhandle(
                    duplicated_handle,
                    os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
                duplicated_handle = None
                descriptor_stat = os.fstat(inherited_fd)
                return (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
                    path_stat.st_dev,
                    path_stat.st_ino,
                )
            finally:
                if inherited_fd is not None:
                    os.close(inherited_fd)
                elif duplicated_handle is not None:
                    _close_windows_handle(duplicated_handle)
        if descriptor is not None:
            descriptor_stat = os.fstat(descriptor)
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                return False
            import fcntl

            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # A previous process in the same transaction may still own
                # the inherited lock while this process validates its handoff.
                pass
            return True
    except (OSError, ValueError, ImportError):
        return False
    return False


def _read_inherited_lock_token(descriptor: int | None, native_handle: int | None) -> str | None:
    """Read the owner token through an inherited descriptor without reopening the lock path."""

    try:
        if os.name == "nt":
            if native_handle is None:
                return None
            duplicated_handle = _duplicate_windows_handle(native_handle)
            inherited_fd: int | None = None
            try:
                import msvcrt

                inherited_fd = msvcrt.open_osfhandle(
                    duplicated_handle,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
                duplicated_handle = None
                os.lseek(inherited_fd, 0, os.SEEK_SET)
                raw = os.read(inherited_fd, 4096)
            finally:
                if inherited_fd is not None:
                    os.close(inherited_fd)
                elif duplicated_handle is not None:
                    _close_windows_handle(duplicated_handle)
        else:
            if descriptor is None:
                return None
            if hasattr(os, "pread"):
                raw = os.pread(descriptor, 4096, 0)
            else:
                duplicate_descriptor = os.dup(descriptor)
                try:
                    os.lseek(duplicate_descriptor, 0, os.SEEK_SET)
                    raw = os.read(duplicate_descriptor, 4096)
                finally:
                    os.close(duplicate_descriptor)
    except (OSError, ValueError, ImportError):
        return None
    return raw.split(b"\n", 1)[0].decode("ascii", errors="ignore").strip()


def install_lock_is_held(home: Path, owner_token: str = "") -> bool:
    """Return whether another process owns the machine-install lock."""

    path = install_lock_path(home)
    ensure_unredirected_directory(path.parent, label="install lock parent")
    inherited_fd, inherited_handle = _inherited_lock_descriptor(home)
    inherited_proof = _inherited_lock_proof(home)
    if os.name == "nt" and inherited_proof is not None:
        return bool(owner_token) and _query_lock_proof(inherited_proof, owner_token)
    if inherited_fd is not None or inherited_handle is not None:
        recorded_value = _read_inherited_lock_token(inherited_fd, inherited_handle)
        if recorded_value is None:
            return False
        token_matches = not owner_token or secrets.compare_digest(recorded_value, owner_token)
        if not token_matches:
            return False
        return _descriptor_matches_lock(path, inherited_fd, inherited_handle)
    try:
        with path.open("a+b") as handle:
            handle.seek(0)
            recorded_value = handle.readline().decode("ascii", errors="ignore").strip()
            token_matches = not owner_token or secrets.compare_digest(recorded_value, owner_token)
            try:
                import fcntl
            except ImportError:
                import msvcrt

                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return token_matches
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                return False
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return token_matches
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except (OSError, ValueError):
        return False


@contextmanager
def install_transaction_lock(home: Path) -> Iterator[None]:
    """Inherit a proven outer lock or acquire the shared machine lock."""

    with install_transaction_locks([home]):
        yield


@contextmanager
def install_transaction_locks(homes: list[Path] | tuple[Path, ...]) -> Iterator[None]:
    """Hold a deterministic set of machine locks for one cross-root transaction."""

    roots = sorted(
        {
            validate_unredirected_path(home, label="Tenetora install home")
            for home in homes
        },
        key=str,
    )
    if not roots:
        raise RuntimeError("machine install transaction requires at least one managed home")
    inherited = os.environ.get("TENETORA_OUTER_INSTALL_LOCK_HELD") == "1"
    if inherited:
        owner_token = os.environ.get(TOKEN_ENV, "")
        if len(owner_token) != TOKEN_PATTERN_LENGTH or any(
            not install_lock_is_held(root, owner_token) for root in roots
        ):
            raise RuntimeError("outer installer lock is not held; refusing to bypass machine install serialization")
        yield
        return
    previous = os.environ.get("TENETORA_OUTER_INSTALL_LOCK_HELD")
    previous_token = os.environ.get(TOKEN_ENV)
    previous_proofs = os.environ.get(LOCK_PROOFS_ENV)
    owner_value = new_install_lock_token()
    with ExitStack() as stack:
        handles: dict[str, object] = {}
        for root in roots:
            handles[str(root)] = stack.enter_context(install_lock(root, owner_value))
        os.environ["TENETORA_OUTER_INSTALL_LOCK_HELD"] = "1"
        os.environ[TOKEN_ENV] = owner_value
        previous_fds = os.environ.get(LOCK_FDS_ENV)
        previous_handles = os.environ.get(LOCK_HANDLES_ENV)
        fd_map = {root: handle.fileno() for root, handle in handles.items()}
        for descriptor in fd_map.values():
            os.set_inheritable(descriptor, True)
        os.environ[LOCK_FDS_ENV] = json.dumps(fd_map, separators=(",", ":"))
        if os.name == "nt":
            import msvcrt

            handle_map = {root: int(msvcrt.get_osfhandle(descriptor)) for root, descriptor in fd_map.items()}
            for native_handle in handle_map.values():
                os.set_handle_inheritable(native_handle, True)
            os.environ[LOCK_HANDLES_ENV] = json.dumps(handle_map, separators=(",", ":"))
        proof_servers: list[tuple[object, threading.Event, threading.Thread]] = []
        try:
            if os.name == "nt":
                proofs: dict[str, str] = {}
                for root in roots:
                    listener, stopped, thread, endpoint = _start_windows_lock_proof_server(owner_value)
                    proof_servers.append((listener, stopped, thread))
                    proofs[str(root)] = endpoint
                os.environ[LOCK_PROOFS_ENV] = json.dumps(proofs, separators=(",", ":"))
            yield
        finally:
            for listener, stopped, thread in proof_servers:
                stopped.set()
                listener.close()
                thread.join(timeout=1)
            if previous is None:
                os.environ.pop("TENETORA_OUTER_INSTALL_LOCK_HELD", None)
            else:
                os.environ["TENETORA_OUTER_INSTALL_LOCK_HELD"] = previous
            if previous_token is None:
                os.environ.pop(TOKEN_ENV, None)
            else:
                os.environ[TOKEN_ENV] = previous_token
            if previous_fds is None:
                os.environ.pop(LOCK_FDS_ENV, None)
            else:
                os.environ[LOCK_FDS_ENV] = previous_fds
            if previous_handles is None:
                os.environ.pop(LOCK_HANDLES_ENV, None)
            else:
                os.environ[LOCK_HANDLES_ENV] = previous_handles
            if previous_proofs is None:
                os.environ.pop(LOCK_PROOFS_ENV, None)
            else:
                os.environ[LOCK_PROOFS_ENV] = previous_proofs
