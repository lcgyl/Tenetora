"""Relate the actual index subject to verified worktree content."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from worktree_fingerprint import _safe_lstat, git_state_entries, run_git


def _digest(content: bytes) -> str:
    return hashlib.sha256(content if b'\0' in content else content.replace(b'\r\n',b'\n')).hexdigest()


def read_index(root:Path)->dict:
    result=run_git(root,'ls-files','--stage','-z',text=False)
    if result.returncode:
        raise ValueError('The Git index could not be read.')
    entries={}
    for record in result.stdout.split(b'\0'):
        if not record:continue
        try:
            header,name=record.split(b'\t',1);mode,object_id,stage=header.decode('ascii').split()
            if stage!='0':raise ValueError('unmerged index')
            entries[os.fsdecode(name)]=(mode,object_id)
        except (ValueError,UnicodeError):
            raise ValueError('The staged subject contains invalid or unmerged index entries.')
    return entries


def index_modes(root:Path,scopes:list[str])->dict:
    return {name:mode for name,(mode,_) in read_index(root).items()
        if not scopes or any(name==scope or name.startswith(scope+'/') for scope in scopes)}


def staged_subject_check(root: Path, paths: list[str], *, verified_index_modes:dict|None=None) -> dict:
    try:entries=read_index(root)
    except ValueError as exc:
        return {'status':'fail','code':'staged-subject-invalid','message':str(exc),'mismatched_paths':paths}
    current=git_state_entries(root,paths)
    if current is None:
        return {'status':'fail','code':'staged-subject-unavailable','message':'The current verified subject could not be read.','mismatched_paths':paths}
    changed=[]
    for name in paths:
        item=entries.get(name);metadata,safe=_safe_lstat(root,name)
        if not safe:
            changed.append(name);continue
        if item is None:
            if metadata is not None:changed.append(name)
            continue
        mode,object_id=item
        if metadata is None or not re.fullmatch(r'[0-9a-f]{40,64}',object_id):
            changed.append(name);continue
        if mode=='160000':
            head=run_git(root/name,'rev-parse','HEAD')
            status=run_git(root/name,'status','--porcelain=v1','--untracked-files=all')
            if not stat.S_ISDIR(metadata.st_mode) or head.returncode or head.stdout.strip()!=object_id or status.returncode or status.stdout.strip():
                changed.append(name)
            continue
        blob=run_git(root,'cat-file','blob',object_id,text=False)
        if blob.returncode:
            changed.append(name);continue
        entry=current.get(name,'').split('\0')
        if mode=='120000':
            matches=stat.S_ISLNK(metadata.st_mode) and len(entry)==4 and entry[1]=='symlink' and entry[3]==hashlib.sha256(blob.stdout).hexdigest()
        elif mode in {'100644','100755'}:
            matches=stat.S_ISREG(metadata.st_mode) and len(entry)==4 and entry[1]=='file' and entry[3]==_digest(blob.stdout)
            if matches and os.name!='nt':
                matches=bool(metadata.st_mode & stat.S_IXUSR)==(mode=='100755')
            elif matches and os.name=='nt':
                # Windows does not expose POSIX executable bits. Preserve the
                # actual index mode observed by the successful run; an untracked
                # new file defaults to Git's non-executable mode.
                matches=mode==(verified_index_modes or {}).get(name,'100644')
        else:matches=False
        if not matches:changed.append(name)
    return {'status':'fail' if changed else 'pass',
        'code':'staged-subject-mismatch' if changed else 'staged-subject-matches-verified-content',
        'message':'Staged bytes, types or executable bits differ from the verified worktree.' if changed else 'The staged subject matches the verified worktree content.',
        'mismatched_paths':sorted(set(changed))}


def observed_index_check(root:Path,subjects:list[dict])->dict:
    """Compare the committed tree with all verified code in each used scope.

    An unchanged HEAD dependency can still differ from the worktree that ran
    the check. Comparing only changed path names would miss that case.
    """
    from verification_plan import classify_path,NON_VERIFICATION_CATEGORIES
    from worktree_fingerprint import _is_volatile_path
    try:index=read_index(root)
    except ValueError as exc:return {'status':'fail','code':'staged-subject-invalid','message':str(exc),'mismatched_paths':[]}
    selected=[];objects=set()
    for subject in subjects:
        entries=subject['source_entries'];scopes=subject['verification_scope']
        paths=set(entries)|{name for name in index if not scopes or any(name==scope or name.startswith(scope+'/') for scope in scopes)}
        for name in paths:
            if _is_volatile_path(name) or classify_path(name) in NON_VERIFICATION_CATEGORIES:continue
            item=index.get(name);selected.append((name,entries.get(name),item,subject['index_modes']))
            if item and item[0] in ('100644','100755','120000'):objects.add(item[1])
    try:digests=_blob_digests(root,objects)
    except (OSError,ValueError) as exc:
        return {'status':'fail','code':'staged-subject-unavailable','message':str(exc),'mismatched_paths':[]}
    changed=[]
    for name,record,item,modes in selected:
        parts=record.split('\0') if isinstance(record,str) else []
        if item is None:
            if len(parts)!=2 or parts[1]!='missing':changed.append(name)
            continue
        mode,object_id=item
        if mode=='160000':
            head=run_git(root/name,'rev-parse','HEAD');status=run_git(root/name,'status','--porcelain=v1','--untracked-files=all')
            if (len(parts)!=4 or parts[1]!='repository' or head.returncode or head.stdout.strip()!=object_id
                or status.returncode or status.stdout.strip()):changed.append(name)
            continue
        digest=digests.get(object_id)
        if len(parts)!=4 or digest is None:
            changed.append(name);continue
        if mode=='120000':matches=parts[1]=='symlink' and parts[3]==digest['raw']
        elif mode in ('100644','100755'):
            matches=parts[1]=='file' and parts[3]==digest['normalized']
            if matches and os.name!='nt':matches=bool(int(parts[2],8)&stat.S_IXUSR)==(mode=='100755')
            elif matches:matches=mode==modes.get(name,'100644')
        else:matches=False
        if not matches:changed.append(name)
    return {'status':'fail' if changed else 'pass','code':'staged-snapshot-mismatch' if changed else 'staged-snapshot-verified',
        'message':'The index tree differs from the code snapshot that actually ran.' if changed else 'The index code matches the observed source snapshot.',
        'mismatched_paths':sorted(set(changed))}


def _blob_digests(root:Path,objects:set[str])->dict:
    import subprocess
    import threading
    from worktree_fingerprint import git_repository_env
    if not objects:return {}
    if any(not re.fullmatch(r'[0-9a-f]{40,64}',item) for item in objects):raise ValueError('Invalid staged object identity.')
    process=subprocess.Popen(['git','-C',str(root),'cat-file','--batch'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=git_repository_env())
    ordered=sorted(objects);failed=[]
    def send():
        try:process.stdin.write(('\n'.join(ordered)+'\n').encode('ascii'));process.stdin.close()
        except OSError:failed.append(True)
    def drain():
        while process.stderr.read(65536):pass
    sender=threading.Thread(target=send,daemon=True);errors=threading.Thread(target=drain,daemon=True)
    sender.start();errors.start();timer=threading.Timer(30,process.kill);timer.daemon=True;timer.start();result={}
    try:
        for object_id in ordered:
            header=process.stdout.readline(256).decode('ascii').split()
            if len(header)!=3 or header[:2]!=[object_id,'blob']:raise ValueError('A staged blob could not be read.')
            remaining=int(header[2]);raw=hashlib.sha256();text=hashlib.sha256();nul=False;pending=False
            while remaining:
                chunk=process.stdout.read(min(1024*1024,remaining))
                if not chunk:raise ValueError('A staged blob was truncated.')
                remaining-=len(chunk);raw.update(chunk);nul=nul or b'\0' in chunk
                if pending:chunk=b'\r'+chunk
                pending=chunk.endswith(b'\r')
                if pending:chunk=chunk[:-1]
                text.update(chunk.replace(b'\r\n',b'\n'))
            if pending:text.update(b'\r')
            if process.stdout.read(1)!=b'\n':raise ValueError('The staged object stream is invalid.')
            result[object_id]={'raw':raw.hexdigest(),'normalized':raw.hexdigest() if nul else text.hexdigest()}
        sender.join(5)
        if failed or process.wait(timeout=5)!=0:raise ValueError('Git could not finish reading the staged subject.')
    except (UnicodeError,subprocess.TimeoutExpired) as exc:raise ValueError('Git staged subject validation failed.') from exc
    finally:
        timer.cancel()
        if process.poll() is None:process.kill();process.wait(timeout=5)
        process.stdout.close();errors.join(5);process.stderr.close()
    return result
