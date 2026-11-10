"""Shared explicit check execution with bounded, redacted local output."""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from harness_io import atomic_write_bytes
from install_progress import redact
from path_security import ensure_unredirected_directory
from worktree_fingerprint import git_repository_env

MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_REDACTION_WINDOW = 4 * 1024 * 1024
SENSITIVE_ENV = re.compile(r'(?i)(?:token|secret|password|passwd|credential|api[_-]?key|access[_-]?key)')
PRIVATE_KEY = re.compile(r'-----BEGIN[^\r\n]*PRIVATE KEY-----.*?(?:-----END[^\r\n]*PRIVATE KEY-----|\Z)',re.DOTALL)


def sanitized_output(content: bytes, environment: dict[str,str]) -> bytes:
    text=content.decode('utf-8',errors='replace')
    text=PRIVATE_KEY.sub('[REDACTED_PRIVATE_KEY]',redact(text))
    values={value for name,value in environment.items() if SENSITIVE_ENV.search(name) and isinstance(value,str) and value}
    if values:
        # One substitution pass prevents short credentials from repeatedly
        # matching and expanding the redaction marker written by earlier ones.
        secrets=re.compile('|'.join(re.escape(value) for value in sorted(values,key=len,reverse=True)))
        text=secrets.sub('[REDACTED]',text)
    return text.encode('utf-8')


def _stop_process(process: subprocess.Popen) -> None:
    if os.name=='nt':
        # This pid belongs to the process started immediately above. Tree
        # termination is needed because descendants may retain the log pipes.
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,check=False,timeout=10)
    else:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    if process.poll() is None:process.kill()


def run_process(root: Path, command: str | list[str], directory: Path, timeout: float,
                *, environment: dict[str,str] | None = None) -> dict:
    """Execute user-requested argv without implicit shell; preserve actual exit."""
    if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not 0<timeout<=86400:
        raise ValueError('Verification timeout must be between 0 and 86400 seconds.')
    argv=shlex.split(command) if isinstance(command,str) else list(command)
    if not argv or any(not isinstance(arg,str) or '\0' in arg for arg in argv):
        raise ValueError('Verification requires valid non-empty argv.')
    ensure_unredirected_directory(directory,label='verification logs')
    directory.chmod(0o700)
    env=git_repository_env() if environment is None else environment.copy()
    redaction_window=max((len(value.encode('utf-8',errors='replace')) for name,value in env.items()
        if SENSITIVE_ENV.search(name) and isinstance(value,str) and value),default=0)+512
    if redaction_window>MAX_REDACTION_WINDOW:raise ValueError('Sensitive verification environment exceeds the logging budget.')
    retain_bytes=MAX_STREAM_BYTES+redaction_window
    output={'stdout':bytearray(),'stderr':bytearray()}
    counts={'stdout':0,'stderr':0}
    streams={}
    started=time.monotonic();timed_out=False;startup_error=False;code=None
    def collect(name,handle):
        try:
            while True:
                chunk=handle.read(65536)
                if not chunk:break
                counts[name]+=len(chunk)
                remaining=retain_bytes-len(output[name])
                if remaining>0:output[name].extend(chunk[:remaining])
        finally:handle.close()
    kwargs={'cwd':root,'env':env,'stdin':subprocess.DEVNULL,'stdout':subprocess.PIPE,'stderr':subprocess.PIPE}
    if os.name=='nt':kwargs['creationflags']=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0)
    else:kwargs['start_new_session']=True
    process=None
    try:
        process=subprocess.Popen(argv,**kwargs)
        for name,handle in [('stdout',process.stdout),('stderr',process.stderr)]:
            thread=threading.Thread(target=collect,args=(name,handle),daemon=True);thread.start();streams[name]=thread
        try:code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True;_stop_process(process);code=process.wait(timeout=10)
        # A short-lived parent must not leave descendants holding output open.
        for thread in streams.values():thread.join(timeout=1)
        if any(thread.is_alive() for thread in streams.values()):
            timed_out=True;_stop_process(process)
            for thread in streams.values():thread.join(timeout=3)
    except (OSError,ValueError) as exc:
        startup_error=True;code=-1;output['stderr'].extend(str(exc).encode('utf-8',errors='replace'))
    finally:
        if process is not None and process.poll() is None:
            _stop_process(process);process.wait(timeout=10)
    logs={}
    for name in ('stdout','stderr'):
        content=sanitized_output(bytes(output[name]),env)
        if len(content)>MAX_STREAM_BYTES:content=content[:MAX_STREAM_BYTES]
        if counts[name]>MAX_STREAM_BYTES:content+=b'\n[Tenetora verification output truncated]\n'
        path=directory/(name+'.txt');atomic_write_bytes(path,content,mode=0o600);path.chmod(0o600)
        logs[name]={'path':path,'sha256':hashlib.sha256(content).hexdigest(),
            'bytes':len(content),'observed_bytes':counts[name], 'truncated':counts[name]>MAX_STREAM_BYTES}
    return {'exit_code':code,'duration_ms':round((time.monotonic()-started)*1000),
        'timed_out':timed_out,'startup_error':startup_error,'logs':logs}
