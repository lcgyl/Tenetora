#!/usr/bin/env python3
"""Safely inspect and remove Tenetora-managed releases and installation state."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from path_security import (
    is_redirected_path,
    remove_unredirected_entry,
    validate_unredirected_path,
)
from release_store import (
    ReleaseStoreError,
    managed_release,
    read_release_manifest,
)
from release_version import version_key


def load_installer_module():
    """Load the packaged installer to reuse its profile helpers verbatim."""

    path = SCRIPT_DIR / "install-skill.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("tenetora_maintenance_installer", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - maintenance must not fail on an optional helper
        return None
    return module


def dsh_cleanup_plan(home: Path) -> list[dict[str, object]]:
    """Profiles whose bundle points into the managed home being removed."""

    installer = load_installer_module()
    if installer is None:
        return []
    profiles_root = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")).expanduser() / "profiles"
    validate_unredirected_path(profiles_root,label='DSH profiles')
    if not profiles_root.is_dir():
        return []
    plans: list[dict[str, object]] = []
    for profile in sorted(profiles_root.iterdir()):
        if not profile.is_dir() and not is_redirected_path(profile):
            continue
        try:
            validate_unredirected_path(profile,label='DSH profile')
            from path_security import validate_unredirected_file_path
            patch=installer.dsh_patch_path(profile)
            for path in (profile/'package.json',patch):
                if path.exists() or is_redirected_path(path):
                    validate_unredirected_file_path(path,label='DSH profile configuration')
            if not installer.dsh_profile_references_home(profile, home):
                continue
            if patch.is_file():
                validate_unredirected_path(patch.parent,label='DSH patch parent')
                validate_unredirected_file_path(patch,label='DSH patch')
                content=patch.read_text(encoding='utf-8')
                if installer.DSH_PATCH_START in content and installer.remove_dsh_patch_block(content) is None:
                    plans.append({'profile':str(profile),'status':'conflict','actions':[], 'warnings':['Managed DSH patch is incomplete; profile and machine files were preserved.']});continue
            plans.append(installer.remove_cordis_bundle(profile.name, dry_run=True))
        except (OSError, RuntimeError, ValueError) as exc:
            plans.append({"profile": str(profile), "status": "error", "actions": [], "warnings": [str(exc)]})
    return plans


def apply_dsh_cleanup(plans: list[dict[str, object]], home: Path | None = None) -> list[dict[str, object]]:
    installer = load_installer_module()
    if installer is None:
        return plans
    applied: list[dict[str, object]] = []
    for plan in plans:
        profile = Path(str(plan.get("profile", "")))
        if not profile.name:
            applied.append(plan)
            continue
        try:
            result=installer.remove_cordis_bundle(profile.name, dry_run=False)
            if home is not None and result.get('status') in {'removed','absent','profile-missing'} and installer.dsh_profile_references_home(profile,home):
                result={**result,'status':'conflict','warnings':['DSH profile still references this machine home after cleanup.']}
            applied.append(result)
        except (OSError, RuntimeError, ValueError) as exc:
            applied.append({"profile": str(profile), "status": "error", "actions": [], "warnings": [str(exc)]})
    return applied



def language_is_zh() -> bool:
    return os.environ.get("TENETORA_LANG", "").lower() in {"zh", "zh-cn", "zh-hans", "中文"}


def text(en: str, zh: str) -> str:
    return zh if language_is_zh() else en


def configured_home() -> Path:
    raw = os.environ.get("TENETORA_HOME")
    if raw:
        return validate_unredirected_path(raw, label="TENETORA_HOME")
    user_home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(Path.home())
    return validate_unredirected_path(Path(user_home) / ".tenetora", label="Tenetora home")


def release_entries(home: Path) -> list[tuple[Path, dict[str, object]]]:
    root = home / "releases"
    if not root.exists():
        return []
    if is_redirected_path(root) or not root.is_dir():
        raise ReleaseStoreError("release storage is not a regular managed directory")
    result: list[tuple[Path, dict[str, object]]] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith("."):
            continue
        if not entry.is_dir() or is_redirected_path(entry):
            raise ReleaseStoreError(f"refusing to inspect unsafe release entry: {entry.name}")
        if not managed_release(entry, home):
            raise ReleaseStoreError(f"refusing to remove unowned or invalid release: {entry.name}")
        result.append((entry, read_release_manifest(entry)))
    return result


def current_target(home: Path) -> Path | None:
    current = home / "current"
    if not (current.exists() or is_redirected_path(current)):
        return None
    try:
        target = current.resolve(strict=True)
    except OSError as exc:
        raise ReleaseStoreError("current pointer is unreadable") from exc
    return target


def cleanup_plan(home: Path) -> dict[str, object]:
    entries = release_entries(home)
    current = current_target(home)
    stable = [item for item in entries if item[1].get("channel", "stable") == "stable"]
    preview = [item for item in entries if item[1].get("channel", "stable") == "preview"]
    keep: set[Path] = set()
    if current is not None:
        keep.add(current.resolve(strict=False))
    for group in (stable, preview):
        if group:
            keep.add(max(group, key=lambda item: version_key(str(item[1]["version"])))[0].resolve(strict=False))
    remove = [path.resolve(strict=False) for path, _ in entries if path.resolve(strict=False) not in keep]
    return {
        "home": str(home),
        "current": str(current) if current else None,
        "keep": sorted(str(path) for path in keep),
        "remove": sorted(str(path) for path in remove),
    }


def apply_cleanup(plan: dict[str, object]) -> list[str]:
    removed: list[str] = []
    for raw in plan.get("remove", []):
        path = Path(str(raw))
        remove_unredirected_entry(path, label="managed release cleanup")
        removed.append(str(path))
    return removed


def uninstall_plan(home: Path, *, purge: bool, project: Path | None = None, include_host_integrations: bool = False) -> dict[str, object]:
    from installation_registry import load_registry
    import maintenance_objects
    import maintenance_hooks
    registry=load_registry(home)
    projects=[Path(str(item['path'])) for item in registry['projects']]
    if project is not None and (project/'.tenetora').is_dir():projects.append(project)
    installer=load_installer_module()
    if installer is None and (home.exists() or is_redirected_path(home)):
        raise ReleaseStoreError('Installer helper is unavailable; host dependencies cannot be inspected. Machine files were preserved. Repair the Tenetora package and retry.')
    objects=maintenance_objects.plan(home,purge=purge,installer=installer)
    import maintenance_native
    native=maintenance_native.plan(home,objects['native_payloads'],installer,project or Path.cwd(),owned_roots=maintenance_objects.releases(home),contexts=projects) if installer is not None else {'actions':[],'preserved':[]}
    hook_plan=maintenance_hooks.plan(projects,home,installer)
    dsh=dsh_cleanup_plan(home)
    if any(item.get('status') not in {'planned','absent','profile-missing'} for item in dsh):
        raise ReleaseStoreError('A DSH profile could not be inspected; machine files were preserved.')
    return {'home':str(home),'purge':purge,**objects,'hook_plan':hook_plan,
        'dsh':dsh,'native':native,'include_host_integrations':include_host_integrations,'project_discovery':'registered projects plus explicit project context; no user-home scan'}


def apply_uninstall(plan: dict[str, object]) -> dict[str, object]:
    from tenetora.install_lock import install_transaction_lock
    from runtime_transaction import runtime_operation_lock
    import maintenance_objects
    import maintenance_hooks
    home=Path(str(plan['home']));staged=[];quarantine=None
    installer=load_installer_module()
    if installer is None and (home.exists() or is_redirected_path(home)):
        raise ReleaseStoreError('Installer helper is unavailable; uninstall was blocked before any host or machine changes.')
    with install_transaction_lock(home),runtime_operation_lock(home/'bin',outside_root=True):
        maintenance_objects.validate(plan)
        maintenance_hooks.validate(plan['hook_plan'])
        import maintenance_native
        if (plan.get('native',{}).get('actions') or plan['hook_plan'].get('integrations')) and not plan.get('include_host_integrations'):
            raise ReleaseStoreError('Host integrations still reference this home. Review the plan and explicitly use --include-host-integrations to detach them.')
        native_removed=maintenance_native.apply(plan['native'],installer) if plan.get('native',{}).get('actions') else []
        dsh=apply_dsh_cleanup(list(plan.get('dsh') or []),home)
        if any(item.get('status') not in {'removed','absent','profile-missing'} for item in dsh):
            raise ReleaseStoreError('DSH cleanup failed; the machine home was preserved.')
        if not plan['remove']:return {'removed':[],'hooks':{'restored_hooks':[]},'warnings':[]}
        quarantine=Path(tempfile.mkdtemp(prefix='.'+home.name+'.uninstall-',dir=home.parent))
        try:
            for index,raw in enumerate(plan['remove']):
                source=Path(str(raw));expected=plan['snapshots'][str(source)]
                if maintenance_objects.snapshot(source)!=expected:raise ReleaseStoreError('Machine object changed before removal.')
                destination=quarantine/str(index);staged.append((source,destination,expected));os.replace(source,destination)
            for source,destination,expected in staged:
                if source.exists() or is_redirected_path(source) or maintenance_objects.snapshot(destination)!=expected:
                    raise ReleaseStoreError('An uninstall object changed or was recreated during staging.')
            hooks=maintenance_hooks.apply(plan['hook_plan'])
        except (OSError,RuntimeError,ValueError):
            conflicts=[]
            for source,destination,expected in reversed(staged):
                try:
                    if (source.exists() or is_redirected_path(source)) and not destination.exists() and not is_redirected_path(destination):
                        if maintenance_objects.snapshot(source)==expected:continue
                    elif not source.exists() and not is_redirected_path(source) and maintenance_objects.snapshot(destination)==expected:
                        os.replace(destination,source);continue
                    conflicts.append(str(source))
                except (OSError,RuntimeError,ValueError):conflicts.append(str(source))
            if conflicts:raise ReleaseStoreError('Uninstall rollback preserved concurrent edits; recovery objects remain at '+str(quarantine))
            if quarantine.exists():quarantine.rmdir()
            raise
    warnings=[]
    try:remove_unredirected_entry(quarantine,label='committed uninstall quarantine')
    except (OSError,RuntimeError):
        warnings.append({'code':'uninstall-committed-cleanup-incomplete','recovery_path':str(quarantine)})
    return {'removed':[str(source) for source,_,_ in staged],'hooks':hooks,'warnings':warnings,'dsh':dsh,'native':native_removed}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Inspect or remove managed Tenetora releases")
    sub = root.add_subparsers(dest="operation", required=True)
    for name in ("cleanup", "uninstall"):
        command = sub.add_parser(name)
        command.add_argument("--home", type=Path, default=None)
        if name=='uninstall':command.add_argument('--include-host-integrations',action='store_true',help='Detach ownership-proven native host references before removing this machine home')
        command.add_argument('--cache',action='store_true',help='cleanup: read-only project cache inventory and removal preview')
        command.add_argument('--path',type=Path,default=Path.cwd(),help='Project context for cache inventory or explicitly registered hooks')
        command.add_argument('--dry-run',action='store_true',help='Display the plan without changing files')
        command.add_argument("--apply", action="store_true", help="apply the displayed plan")
        command.add_argument("--yes", action="store_true", help="confirm destructive changes")
        command.add_argument("--purge", action="store_true", help="uninstall: remove the whole managed home")
        command.add_argument("--json", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.dry_run and args.apply:raise ReleaseStoreError('--dry-run cannot be combined with --apply')
        if args.cache:
            if args.operation!='cleanup' or args.home is not None or args.apply or args.purge:
                raise ReleaseStoreError('Project cache supports read-only cleanup planning; do not combine it with machine-home, purge or apply options.')
            from cache_inventory import inventory
            payload=inventory(args.path)
            if args.json:print(json.dumps(payload,ensure_ascii=False,sort_keys=True))
            else:
                print(text('Project cache inventory (read-only)','项目缓存盘点（只读）'))
                for item in payload['entries']:print(item['path']+': '+str(item['logical_bytes'])+' bytes; '+'; '.join(item['keep_reasons']))
                for item in payload['candidates']:print(text('Review candidate: ','待复核候选：')+item['path'])
            return 0
        home = validate_unredirected_path(args.home or configured_home(), label="Tenetora home")
        plan = cleanup_plan(home) if args.operation == "cleanup" else uninstall_plan(home, purge=args.purge, project=args.path, include_host_integrations=getattr(args,'include_host_integrations',False))
        if args.apply and not args.yes:
            raise ReleaseStoreError("destructive maintenance requires --yes")
        removed: list[str] = []
        if args.apply:
            if args.operation=='cleanup':removed=apply_cleanup(plan)
            else:
                removal=apply_uninstall(plan);removed=removal['removed'];plan['hook_result']=removal['hooks'];plan['warnings']=removal['warnings'];plan['dsh']=removal.get('dsh',[])
    except (OSError, RuntimeError, ReleaseStoreError, ValueError, subprocess.SubprocessError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        else:
            print(text(f"Maintenance blocked: {exc}", f"维护操作已阻断：{exc}"), file=sys.stderr)
        return 1
    payload = {"status": "applied" if args.apply else "planned", "operation": args.operation, **plan}
    if args.apply:
        payload["removed"] = removed
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(text(f"{args.operation} plan for {home}", f"{args.operation} 计划：{home}"))
        for path in payload.get("remove", []):
            print(text(f"  {'removed' if args.apply else 'would remove'} {path}", f"  {'已删除' if args.apply else '将删除'} {path}"))
        for entry in payload.get("dsh", []):
            print(
                text(
                    f"  DSH profile {entry.get('profile')}: {entry.get('status')}",
                    f"  DSH profile {entry.get('profile')}：{entry.get('status')}",
                )
            )
            for action in entry.get("actions", []):
                print(f"    {action}")
            for warning in entry.get("warnings", []):
                print(text(f"    warning: {warning}", f"    警告：{warning}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
