"""Observed verification runs and exact, source-bound reusable receipts."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shlex
import sys
import uuid
from pathlib import Path,PureWindowsPath

from governance_trail import append_event,load_retained_trail,require_writable_history
from harness_io import atomic_write_text
from path_security import ensure_unredirected_directory,validate_unredirected_file_path,harness_missing_message
from verification_executor import run_process
from worktree_fingerprint import WORKTREE_FINGERPRINT_VERSION,git_state_entries,git_state_fingerprint,normalize_scopes,snapshot_fingerprint

RUN_ID=re.compile(r'vr-[0-9a-f]{32}')
ROOT_REL=Path('.tenetora/state/verification-runs')
REFERENCE_FIELDS=('execution_receipt','execution_receipt_sha256','execution_run_id',
    'execution_origin_event_id','execution_origin_sequence','execution_assurance')


def same_execution(left:dict,right:dict)->bool:
    """Old claims without assurance are declarations, never observed runs."""
    return all(left.get(key,'declaration' if key=='execution_assurance' else None)
        ==right.get(key,'declaration' if key=='execution_assurance' else None) for key in REFERENCE_FIELDS)


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Execution receipt contains duplicate JSON fields.')
        result[key]=value
    return result


def root_fingerprint(root):
    return hashlib.sha256(str(root.resolve()).encode('utf-8',errors='surrogatepass')).hexdigest()


def run_path(root:Path,run_id:str)->Path:
    if not isinstance(run_id,str) or not RUN_ID.fullmatch(run_id):raise ValueError('Invalid verification run identity.')
    return validate_unredirected_file_path(root/ROOT_REL/run_id/'receipt.json',label='execution receipt')


def execute_verification(root:Path,command:str|list[str],scopes:list[str],timeout:float,*,known_paths=())->dict:
    """Explicit execution only; callers never invoke this while importing reports."""
    if not (root/'.tenetora').is_dir():raise ValueError(harness_missing_message(root))
    normalized=list(normalize_scopes(scopes))
    text=command if isinstance(command,str) else shlex.join(command)
    if not text.strip():raise ValueError('A verification command is required.')
    argv=shlex.split(command) if isinstance(command,str) else list(command)
    if not argv or any(not isinstance(arg,str) or '\0' in arg for arg in argv):raise ValueError('Verification argv is invalid.')
    if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not 0<timeout<=86400:raise ValueError('Invalid verification timeout.')
    source_entries=git_state_entries(root,normalized,known_paths=known_paths)
    if source_entries is None:raise ValueError('Verification requires a readable Git source subject.')
    before=snapshot_fingerprint(source_entries,normalized)
    require_writable_history(root)
    run_id='vr-'+uuid.uuid4().hex;path=run_path(root,run_id)
    ensure_unredirected_directory(path.parent,label='verification run');path.parent.chmod(0o700)
    from staged_evidence import index_modes
    observed_modes=index_modes(root,normalized)
    command_hash=hashlib.sha256(text.encode('utf-8')).hexdigest()
    started=dt.datetime.now(dt.timezone.utc).isoformat()
    event={'type':'verification-execution-start','status':'pending','execution_run_id':run_id,
        'verification_scope':normalized,'project_root_fingerprint':root_fingerprint(root),
        'verification_command_hash':command_hash,'verification_command_hash_version':2}
    append_event(root,event)
    process=run_process(root,command,path.parent/'logs',timeout)
    after=git_state_fingerprint(root,normalized,known_paths=source_entries)
    status='passed' if (process['exit_code']==0 and not process['timed_out'] and not process['startup_error'] and after==before) else 'failed'
    reason='source-changed' if after!=before else 'timeout' if process['timed_out'] else 'startup-error' if process['startup_error'] else 'exit-code'
    logs={name:{**record,'path':record['path'].relative_to(root).as_posix()} for name,record in process['logs'].items()}
    receipt={'version':1,'run_id':run_id,'project_root_fingerprint':root_fingerprint(root),
        'command_hash':command_hash,'command_hash_version':2,'command_form':'text' if isinstance(command,str) else 'argv',
        'verification_scope':normalized,'source_fingerprint_version':WORKTREE_FINGERPRINT_VERSION,
        'source_before':before,'source_after':after,'source_entries':source_entries,
        'index_modes':observed_modes,
        'started_at':started,'finished_at':dt.datetime.now(dt.timezone.utc).isoformat(),
        'duration_ms':process['duration_ms'],'exit_code':process['exit_code'],'timed_out':process['timed_out'],
        'startup_error':process['startup_error'],'status':status,'reason':reason,'logs':logs,
        'environment':{'python':'.'.join(map(str,sys.version_info[:3])),'platform':sys.platform}}
    atomic_write_text(path,json.dumps(receipt,sort_keys=True,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
    digest=hashlib.sha256(path.read_bytes()).hexdigest();relative=path.relative_to(root).as_posix()
    end=append_event(root,{'type':'verification-execution','status':'pass' if status=='passed' else 'fail',
        'verification_status':status,'execution_run_id':run_id,'execution_receipt':relative,
        'execution_receipt_sha256':digest,'verification_scope':normalized,
        'verification_command_hash':command_hash,'verification_command_hash_version':2,
        'project_root_fingerprint':receipt['project_root_fingerprint'],'git_state_fingerprint':after,
        'exit_code':process['exit_code'],'duration_ms':process['duration_ms']})
    return {'receipt':receipt,'execution_receipt':relative,'execution_receipt_sha256':digest,
        'execution_run_id':run_id,'execution_origin_event_id':end['event_id'],
        'execution_origin_sequence':end['_journal_sequence'],'execution_assurance':'observed'}


def read_execution(root:Path,reference:str,expected_sha:str|None=None,*,writer_locked:bool=False)->dict:
    """Validate the actual run, immutable bytes, logs and canonical end event."""
    if not isinstance(reference,str) or not reference:raise ValueError('An execution receipt is required.')
    relative=Path(reference.replace('\\','/'))
    if relative.is_absolute() or '..' in relative.parts or PureWindowsPath(reference).drive:
        raise ValueError('Execution receipt must be project relative.')
    generic=relative.parts[:3]==tuple(ROOT_REL.parts) and len(relative.parts)==5 and relative.name=='receipt.json'
    feature=relative.parts[:3]==('.tenetora','.cache','feature-checks') and len(relative.parts)==5 and relative.name=='receipt.json'
    if not generic and not feature:raise ValueError('Execution receipt is outside the verification run directories.')
    reference=relative.as_posix()
    path=validate_unredirected_file_path(root/relative,label='execution receipt')
    if path.stat().st_size>16*1024*1024:raise ValueError('Execution receipt exceeds its bounded size.')
    raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if expected_sha is not None and digest!=expected_sha:raise ValueError('Execution receipt bytes changed.')
    value=json.loads(raw,object_pairs_hook=unique_object)
    # Feature v2/v3 wrappers may point to the same generic observed run. The
    # feature contract/criterion layer remains separately authoritative.
    if relative.parts[:3]==('.tenetora','.cache','feature-checks'):
        if not isinstance(value,dict) or value.get('version') not in (2,3) or not isinstance(value.get('observed_execution'),dict):
            raise ValueError('Legacy feature receipt has no observed execution reference.')
        nested=value['observed_execution']
        return read_execution(root,nested.get('execution_receipt'),nested.get('execution_receipt_sha256'),writer_locked=writer_locked)
    if not isinstance(value,dict) or type(value.get('version')) is not int or value['version']!=1:raise ValueError('Execution receipt schema is invalid.')
    run_id=value.get('run_id');canonical=run_path(root,run_id)
    if path!=canonical:raise ValueError('Execution receipt locator does not match its run.')
    for key in ('command_hash','project_root_fingerprint','source_before','source_after'):
        if not isinstance(value.get(key),str) or not re.fullmatch(r'[0-9a-f]{64}',value[key]):raise ValueError('Execution receipt contains an invalid digest.')
    if value['project_root_fingerprint']!=root_fingerprint(root) or value.get('command_hash_version')!=2 or value.get('source_fingerprint_version')!=WORKTREE_FINGERPRINT_VERSION:
        raise ValueError('Execution receipt identity is unsupported or belongs to another project.')
    if type(value.get('exit_code')) is not int or type(value.get('timed_out')) is not bool or type(value.get('startup_error')) is not bool:
        raise ValueError('Execution process result has invalid types.')
    if type(value.get('duration_ms')) is not int or value['duration_ms']<0 or value.get('command_form') not in ('text','argv'):
        raise ValueError('Execution duration or command identity is invalid.')
    for key in ('started_at','finished_at'):
        stamp=value.get(key)
        if not isinstance(stamp,str):raise ValueError('Execution timestamps must be strings.')
        try:parsed=dt.datetime.fromisoformat(stamp.replace('Z','+00:00'))
        except ValueError as exc:raise ValueError('Execution timestamp is invalid.') from exc
        if parsed.tzinfo is None:raise ValueError('Execution timestamps require an explicit timezone.')
    entries=value.get('source_entries')
    if not isinstance(entries,dict) or any(not isinstance(key,str) or not isinstance(item,str) for key,item in entries.items()):
        raise ValueError('Execution source entries are invalid.')
    scopes=value.get('verification_scope')
    if not isinstance(scopes,list) or any(not isinstance(item,str) for item in scopes) or list(normalize_scopes(scopes))!=scopes:
        raise ValueError('Execution receipt scope is invalid.')
    if value.get('status')!='passed' or value['exit_code']!=0 or value['timed_out'] or value['startup_error'] or value['source_before']!=value['source_after']:
        raise ValueError('The actual verification execution did not pass on unchanged source.')
    if (snapshot_fingerprint(entries,scopes)!=value['source_before']
        or value['source_after']!=git_state_fingerprint(root,scopes,known_paths=entries)):
        raise ValueError('Verified execution source changed.')
    modes=value.get('index_modes')
    if not isinstance(modes,dict) or any(not isinstance(name,str) or mode not in ('100644','100755','120000','160000') for name,mode in modes.items()):
        raise ValueError('Observed index mode metadata is invalid.')
    logs=value.get('logs')
    if not isinstance(logs,dict) or set(logs)!={'stdout','stderr'}:raise ValueError('Execution logs are incomplete.')
    for name,record in logs.items():
        expected=path.parent/'logs'/(name+'.txt')
        if not isinstance(record,dict) or record.get('path')!=expected.relative_to(root).as_posix():raise ValueError('Execution log locator is invalid.')
        validate_unredirected_file_path(expected,label='execution log')
        if expected.stat().st_size>2*1024*1024+256:raise ValueError('Execution log exceeds its bounded size.')
        data=expected.read_bytes()
        if len(data)>2*1024*1024+256 or hashlib.sha256(data).hexdigest()!=record.get('sha256'):
            raise ValueError('Execution logs changed or exceed their bounded size.')
        if (type(record.get('bytes')) is not int or record['bytes']!=len(data)
            or type(record.get('observed_bytes')) is not int or record['observed_bytes']<0
            or type(record.get('truncated')) is not bool):raise ValueError('Execution log metadata is invalid.')
    history=load_retained_trail(root,project=False,writer_locked=writer_locked)
    if history.get('invalid') or history.get('conflicts'):raise ValueError('Execution history is invalid or conflicting.')
    event=next((event for event in history['events'] if event.get('type')=='verification-execution'
        and event.get('execution_run_id')==run_id and event.get('execution_receipt')==reference
        and event.get('execution_receipt_sha256')==digest),None)
    if not event or not isinstance(event.get('event_id'),str) or not event['event_id'] or type(event.get('_journal_sequence')) is not int or event['_journal_sequence']<1:
        raise ValueError('The canonical observed execution fact is missing.')
    if (event.get('status')!='pass' or event.get('verification_status')!='passed'
        or type(event.get('exit_code')) is not int or event['exit_code']!=value['exit_code']
        or event.get('project_root_fingerprint')!=value['project_root_fingerprint']
        or type(event.get('verification_command_hash_version')) is not int or event['verification_command_hash_version']!=value['command_hash_version']
        or event.get('verification_scope')!=scopes or event.get('verification_command_hash')!=value['command_hash']
        or event.get('git_state_fingerprint')!=value['source_after']):
        raise ValueError('Observed execution event does not match its receipt.')
    start=next((start for start in history['events'] if start.get('type')=='verification-execution-start'
        and start.get('execution_run_id')==run_id),None)
    from claim_evidence import execution_pair_passed
    if not execution_pair_passed(start,event):
        raise ValueError('The observed execution start/end facts are inconsistent.')
    return {'receipt':value,'execution_receipt':reference,'execution_receipt_sha256':digest,
        'execution_run_id':run_id,'execution_origin_event_id':event['event_id'],
        'execution_origin_sequence':event['_journal_sequence'],'execution_assurance':'observed'}


def claim_execution_problem(root:Path,event:dict,*,required:bool=False,writer_locked:bool=False)->str|None:
    if event.get('execution_assurance')!='observed':
        return 'observed-execution-required' if required else None
    try:
        observed=read_execution(root,event.get('execution_receipt'),event.get('execution_receipt_sha256'),writer_locked=writer_locked)
        receipt=observed['receipt']
        if any(event.get(key)!=observed[key] for key in REFERENCE_FIELDS):return 'execution-reference-mismatch'
        if event.get('verification_command_hash')!=receipt['command_hash'] or event.get('verification_scope',[])!=receipt['verification_scope']:
            return 'execution-subject-mismatch'
    except (OSError,RuntimeError,ValueError,TypeError):return 'execution-receipt-invalid'
    return None


def mirror_logs(root:Path,receipt:dict,directory:Path)->dict:
    """Preserve feature v2/v3 log locators using already redacted observed bytes."""
    from harness_io import atomic_write_bytes
    ensure_unredirected_directory(directory,label='feature verification log projection')
    logs={}
    for name,record in receipt['logs'].items():
        source=validate_unredirected_file_path(root/record['path'],label='observed log source')
        content=source.read_bytes()
        if hashlib.sha256(content).hexdigest()!=record['sha256']:raise ValueError('Observed output changed before projection.')
        target=directory/(name+'.txt');atomic_write_bytes(target,content,mode=0o600)
        logs[name]={'path':target,'sha256':record['sha256']}
    return logs
