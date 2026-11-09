"""Run an explicitly requested verification and return an observed receipt."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR=Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:sys.path.insert(0,str(SCRIPT_DIR))

from execution_receipts import execute_verification  # noqa: E402
from path_security import validate_existing_project_path  # noqa: E402


def existing_project(raw):
    try:return validate_existing_project_path(raw)
    except RuntimeError as exc:raise argparse.ArgumentTypeError(str(exc)) from exc


def main(argv=None):
    parser=argparse.ArgumentParser(prog='tenetora verify')
    sub=parser.add_subparsers(dest='action',required=True)
    run=sub.add_parser('run',help='Explicitly execute a check and record source-bound observed evidence.')
    run.add_argument('--path','-p',type=existing_project,default=Path.cwd())
    run.add_argument('--scope',action='append',default=[],help='Project-relative scope; omitted means the whole project.')
    run.add_argument('--timeout',type=float,default=600)
    run.add_argument('--command',help='Exact command text parsed as argv without an implicit shell.')
    run.add_argument('--json',action='store_true')
    run.add_argument('argv',nargs=argparse.REMAINDER,help='Command argv after --; no command executes during ordinary claim or report import.')
    args=parser.parse_args(argv)
    values=args.argv[1:] if args.argv[:1]==['--'] else args.argv
    if bool(args.command)==bool(values):parser.error('Supply exactly one --command text or command argv after --.')
    try:
        observed=execute_verification(args.path,args.command if args.command is not None else values,args.scope,args.timeout)
        receipt=observed.pop('receipt')
        payload={'status':'pass' if receipt['status']=='passed' else 'fail',
            'code':'verification-execution-passed' if receipt['status']=='passed' else 'verification-execution-failed',
            'exit_code':receipt['exit_code'],'timed_out':receipt['timed_out'],'startup_error':receipt['startup_error'],
            'reason':receipt['reason'],'duration_ms':receipt['duration_ms'],
            'verification_command_hash':receipt['command_hash'],'verification_scope':receipt['verification_scope'],**observed}
    except (OSError,RuntimeError,ValueError,TypeError) as exc:
        payload={'status':'fail','code':'verification-execution-unavailable','message':str(exc)}
    if args.json:print(json.dumps(payload,ensure_ascii=False,indent=2))
    else:
        print('Verification: '+payload['status'])
        if payload.get('execution_receipt'):print('Observed receipt: '+payload['execution_receipt'])
        if payload.get('message'):print(payload['message'],file=sys.stderr)
    if payload['status']=='pass':return 0
    exit_code=payload.get('exit_code')
    return exit_code if type(exit_code) is int and 0<exit_code<126 else 1


if __name__=='__main__':raise SystemExit(main())
