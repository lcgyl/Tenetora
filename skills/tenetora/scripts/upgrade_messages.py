"""Stable upgrade error message codes and translations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MessageTemplate:
    code: str
    english: str
    chinese: str


class MessageIssue(str):
    """A string-compatible producer message carrying a stable identity."""

    def __new__(cls, code: str, fallback: str = '', *, parameters: dict[str, object] | None = None):
        values = dict(parameters or {})
        template = MESSAGE_TEMPLATES.get(code)
        source = template.english if template else fallback or code
        try:
            text = source.format_map(values)
        except (KeyError, ValueError):
            text = fallback or source
        result = super().__new__(cls, text)
        result.code = code
        result.fallback = fallback
        result.parameters = values
        return result


MESSAGE_TEMPLATES = {
    'upgrade.machine-wide-upgrade-preflight-is-blocked': MessageTemplate('upgrade.machine-wide-upgrade-preflight-is-blocked', 'machine-wide upgrade preflight is blocked', '机器级升级预检被阻断'),
    'upgrade.release-download-failed': MessageTemplate('upgrade.release-download-failed', 'release download failed', '发布包下载失败'),
    'upgrade.preview-channel-unavailable': MessageTemplate('upgrade.preview-channel-unavailable', 'preview channel pointer is unavailable', '预览渠道指针不存在'),
    'upgrade.release-source-options-are-mutually-exclusive': MessageTemplate('upgrade.release-source-options-are-mutually-exclusive', 'release source options are mutually exclusive', '发布源选项互斥'),
    'upgrade.release-manifest-and-zip-urls-must-be-supplied-together': MessageTemplate('upgrade.release-manifest-and-zip-urls-must-be-supplied-together', 'release manifest and ZIP URLs must be supplied together', '发布清单地址和 ZIP 地址必须成对提供'),
    'upgrade.release-mirror-url-is-invalid': MessageTemplate('upgrade.release-mirror-url-is-invalid', 'release mirror URL is invalid', '发布镜像地址无效'),
    'upgrade.release-mirror-url-must-use-an-https-url': MessageTemplate('upgrade.release-mirror-url-must-use-an-https-url', 'release mirror URL must use an HTTPS URL', '发布镜像地址必须使用 HTTPS'),
    'upgrade.release-mirror-url-must-not-contain-credentials': MessageTemplate('upgrade.release-mirror-url-must-not-contain-credentials', 'release mirror URL must not contain credentials', '发布镜像地址不能包含凭证'),
    'upgrade.release-mirror-url-must-not-contain-a-query-or-fragment': MessageTemplate('upgrade.release-mirror-url-must-not-contain-a-query-or-fragment', 'release mirror URL must not contain a query or fragment', '发布镜像地址不能包含 query 或 fragment'),
    'upgrade.release-manifest-url-is-invalid': MessageTemplate('upgrade.release-manifest-url-is-invalid', 'release manifest URL is invalid', '发布清单地址无效'),
    'upgrade.release-manifest-url-must-use-an-https-url': MessageTemplate('upgrade.release-manifest-url-must-use-an-https-url', 'release manifest URL must use an HTTPS URL', '发布清单地址必须使用 HTTPS'),
    'upgrade.release-manifest-url-must-not-contain-credentials': MessageTemplate('upgrade.release-manifest-url-must-not-contain-credentials', 'release manifest URL must not contain credentials', '发布清单地址不能包含凭证'),
    'upgrade.release-manifest-url-must-not-contain-a-query-or-fragment': MessageTemplate('upgrade.release-manifest-url-must-not-contain-a-query-or-fragment', 'release manifest URL must not contain a query or fragment', '发布清单地址不能包含 query 或 fragment'),
    'upgrade.release-zip-url-is-invalid': MessageTemplate('upgrade.release-zip-url-is-invalid', 'release ZIP URL is invalid', '发布包 ZIP 地址无效'),
    'upgrade.release-zip-url-must-use-an-https-url': MessageTemplate('upgrade.release-zip-url-must-use-an-https-url', 'release ZIP URL must use an HTTPS URL', '发布包 ZIP 地址必须使用 HTTPS'),
    'upgrade.release-zip-url-must-not-contain-credentials': MessageTemplate('upgrade.release-zip-url-must-not-contain-credentials', 'release ZIP URL must not contain credentials', '发布包 ZIP 地址不能包含凭证'),
    'upgrade.release-zip-url-must-not-contain-a-query-or-fragment': MessageTemplate('upgrade.release-zip-url-must-not-contain-a-query-or-fragment', 'release ZIP URL must not contain a query or fragment', '发布包 ZIP 地址不能包含 query 或 fragment'),
    'upgrade.release-manifest-exceeds-the-bounded-size-limit': MessageTemplate('upgrade.release-manifest-exceeds-the-bounded-size-limit', 'release manifest exceeds the bounded size limit', '发布清单超过大小上限'),
    'upgrade.release-manifest-must-be-a-json-object': MessageTemplate('upgrade.release-manifest-must-be-a-json-object', 'release manifest must be a JSON object', '发布清单必须是 JSON 对象'),
    'upgrade.release-manifest-identity-or-format-is-invalid': MessageTemplate('upgrade.release-manifest-identity-or-format-is-invalid', 'release manifest identity or format is invalid', '发布清单身份或格式无效'),
    'upgrade.release-commit-identity-is-invalid': MessageTemplate('upgrade.release-commit-identity-is-invalid', 'release commit identity is invalid', '发布提交身份无效'),
    'upgrade.release-installer-protocol-is-older-than-this-upgrader-supports': MessageTemplate('upgrade.release-installer-protocol-is-older-than-this-upgrader-supports', 'release installer protocol is older than this upgrader supports', '发布包安装协议低于当前升级器支持范围'),
    'upgrade.release-bridge-protocol-is-incompatible': MessageTemplate('upgrade.release-bridge-protocol-is-incompatible', 'release bridge protocol is incompatible', '发布包桥接协议不兼容'),
    'upgrade.release-zip-root-is-invalid': MessageTemplate('upgrade.release-zip-root-is-invalid', 'release zip root is invalid', '发布包 ZIP 根目录无效'),
    'upgrade.release-upgrade-entrypoint-is-invalid': MessageTemplate('upgrade.release-upgrade-entrypoint-is-invalid', 'release upgrade entrypoint is invalid', '发布包升级入口无效'),
    'upgrade.release-file-inventory-is-invalid': MessageTemplate('upgrade.release-file-inventory-is-invalid', 'release file inventory is invalid', '发布包文件清单无效'),
    'upgrade.release-file-inventory-contains-duplicates': MessageTemplate('upgrade.release-file-inventory-contains-duplicates', 'release file inventory contains duplicates', '发布包文件清单含重复项'),
    'upgrade.release-file-inventory-must-be-sorted': MessageTemplate('upgrade.release-file-inventory-must-be-sorted', 'release file inventory must be sorted', '发布包文件清单未按稳定顺序排列'),
    'upgrade.release-file-inventory-contains-an-unsafe-path': MessageTemplate('upgrade.release-file-inventory-contains-an-unsafe-path', 'release file inventory contains an unsafe path', '发布包文件清单含不安全路径'),
    'upgrade.release-file-sha256-inventory-does-not-match-files': MessageTemplate('upgrade.release-file-sha256-inventory-does-not-match-files', 'release file SHA256 inventory does not match files', '发布包文件 SHA256 清单与文件列表不一致'),
    'upgrade.release-file-sha256-inventory-is-invalid': MessageTemplate('upgrade.release-file-sha256-inventory-is-invalid', 'release file SHA256 inventory is invalid', '发布包文件 SHA256 清单无效'),
    'upgrade.release-state-schema-contract-is-invalid': MessageTemplate('upgrade.release-state-schema-contract-is-invalid', 'release state schema contract is invalid', '发布包状态 schema 合同无效'),
    'upgrade.release-artifact-name-is-invalid': MessageTemplate('upgrade.release-artifact-name-is-invalid', 'release artifact name is invalid', '发布包产物名称无效'),
    'upgrade.release-sha256-is-invalid': MessageTemplate('upgrade.release-sha256-is-invalid', 'release SHA256 is invalid', '发布包 SHA256 无效'),
    'upgrade.release-zip-has-no-valid-embedded-manifest': MessageTemplate('upgrade.release-zip-has-no-valid-embedded-manifest', 'release ZIP has no valid embedded manifest', '发布包 ZIP 缺少有效的内嵌清单'),
    'upgrade.release-zip-file-count-is-invalid': MessageTemplate('upgrade.release-zip-file-count-is-invalid', 'release ZIP file count is invalid', '发布包 ZIP 文件数量无效'),
    'upgrade.release-zip-contains-an-unsafe-path': MessageTemplate('upgrade.release-zip-contains-an-unsafe-path', 'release ZIP contains an unsafe path', '发布包 ZIP 含不安全路径'),
    'upgrade.release-zip-contains-duplicate-entries': MessageTemplate('upgrade.release-zip-contains-duplicate-entries', 'release ZIP contains duplicate entries', '发布包 ZIP 含重复条目'),
    'upgrade.release-zip-contains-portable-path-collisions': MessageTemplate('upgrade.release-zip-contains-portable-path-collisions', 'release ZIP contains portable path collisions', '发布包 ZIP 含跨平台路径冲突'),
    'upgrade.release-zip-contains-a-symbolic-link': MessageTemplate('upgrade.release-zip-contains-a-symbolic-link', 'release ZIP contains a symbolic link', '发布包 ZIP 含符号链接'),
    'upgrade.release-zip-contains-a-file-and-directory-path-collision': MessageTemplate('upgrade.release-zip-contains-a-file-and-directory-path-collision', 'release ZIP contains a file and directory path collision', '发布包 ZIP 含文件与目录路径冲突'),
    'upgrade.release-zip-expands-beyond-the-bounded-size-limit': MessageTemplate('upgrade.release-zip-expands-beyond-the-bounded-size-limit', 'release ZIP expands beyond the bounded size limit', '发布包 ZIP 解压后超过大小上限'),
    'upgrade.release-zip-extraction-failed': MessageTemplate('upgrade.release-zip-extraction-failed', 'release ZIP extraction failed', '发布包 ZIP 解压失败'),
    'upgrade.release-zip-root-is-invalid-2': MessageTemplate('upgrade.release-zip-root-is-invalid-2', 'release ZIP root is invalid', '发布包 ZIP 根目录无效'),
    'upgrade.developer-source-must-not-be-a-symbolic-link': MessageTemplate('upgrade.developer-source-must-not-be-a-symbolic-link', 'developer source must not be a symbolic link', '开发源码目录不能是符号链接'),
    'upgrade.developer-source-contains-a-symbolic-link-or-special-file': MessageTemplate('upgrade.developer-source-contains-a-symbolic-link-or-special-file', 'developer source contains a symbolic link or special file', '开发源码含符号链接或特殊文件'),
    'upgrade.release-zip-exceeds-the-bounded-size-limit': MessageTemplate('upgrade.release-zip-exceeds-the-bounded-size-limit', 'release ZIP exceeds the bounded size limit', '发布包 ZIP 超过大小上限'),
    'upgrade.offline-release-zip-requires-sha256-or-a-sibling-sha256-file': MessageTemplate('upgrade.offline-release-zip-requires-sha256-or-a-sibling-sha256-file', 'offline release ZIP requires --sha256 or a sibling .sha256 file', '离线发布包 ZIP 需要 --sha256 或同名 .sha256 文件'),
    'upgrade.release-zip-sha256-mismatch': MessageTemplate('upgrade.release-zip-sha256-mismatch', 'release ZIP SHA256 mismatch', '发布包 ZIP SHA256 不匹配'),
    'upgrade.release-version-is-older-than-the-installed-version': MessageTemplate('upgrade.release-version-is-older-than-the-installed-version', 'release version is older than the installed version', '发布版本低于已安装版本'),
    'upgrade.external-and-embedded-release-manifests-disagree': MessageTemplate('upgrade.external-and-embedded-release-manifests-disagree', 'external and embedded release manifests disagree', '外部发布清单与 ZIP 内嵌清单不一致'),
    'upgrade.release-zip-file-inventory-does-not-match-its-manifest': MessageTemplate('upgrade.release-zip-file-inventory-does-not-match-its-manifest', 'release ZIP file inventory does not match its manifest', '发布包 ZIP 文件列表与清单不一致'),
    'upgrade.release-zip-file-contents-do-not-match-its-manifest': MessageTemplate('upgrade.release-zip-file-contents-do-not-match-its-manifest', 'release ZIP file contents do not match its manifest', '发布包 ZIP 文件内容与清单不一致'),
    'upgrade.acquired-release-contents-do-not-match-its-manifest': MessageTemplate('upgrade.acquired-release-contents-do-not-match-its-manifest', 'acquired release contents do not match its manifest', '获取到的 release 内容与清单不一致'),
    'upgrade.acquired-release-does-not-match-the-selected-manifest': MessageTemplate('upgrade.acquired-release-does-not-match-the-selected-manifest', 'acquired release does not match the selected manifest', '获取到的 release 与所选清单不一致'),
    'upgrade.existing-versioned-release-is-not-a-valid-managed-release': MessageTemplate('upgrade.existing-versioned-release-is-not-a-valid-managed-release', 'existing versioned release is not a valid managed release', '现有版本 release 不是有效的受管 release'),
    'upgrade.versioned-release-is-immutable-and-already-contains-different-content': MessageTemplate('upgrade.versioned-release-is-immutable-and-already-contains-different-content', 'versioned release is immutable and already contains different content', '版本化 release 不可变，但现有内容不同'),
    'upgrade.versioned-release-contents-do-not-match-its-manifest': MessageTemplate('upgrade.versioned-release-contents-do-not-match-its-manifest', 'versioned release contents do not match its manifest', '版本化 release 内容与清单不一致'),
    'upgrade.cannot-create-the-managed-windows-release-junction': MessageTemplate('upgrade.cannot-create-the-managed-windows-release-junction', 'cannot create the managed Windows release junction', '无法创建受管 Windows release junction'),
    'upgrade.managed-pointer-replacement-failed-the-prior-pointer-was-restored': MessageTemplate('upgrade.managed-pointer-replacement-failed-the-prior-pointer-was-restored', 'managed pointer replacement failed; the prior pointer was restored', '受管指针替换失败，旧指针已恢复'),
    'upgrade.release-activation-requires-a-valid-immutable-managed-release': MessageTemplate('upgrade.release-activation-requires-a-valid-immutable-managed-release', 'release activation requires a valid immutable managed release', 'release 激活需要有效且不可变的受管 release'),
    'upgrade.upgrade-transaction-journal-is-unreadable': MessageTemplate('upgrade.upgrade-transaction-journal-is-unreadable', 'upgrade transaction journal is unreadable', '升级事务日志不可读'),
    'upgrade.upgrade-transaction-journal-schema-is-invalid': MessageTemplate('upgrade.upgrade-transaction-journal-schema-is-invalid', 'upgrade transaction journal schema is invalid', '升级事务日志 schema 无效'),
    'upgrade.upgrade-transaction-revision-is-invalid': MessageTemplate('upgrade.upgrade-transaction-revision-is-invalid', 'upgrade transaction revision is invalid', '升级事务 revision 无效'),
    'upgrade.an-unfinished-upgrade-transaction-already-exists': MessageTemplate('upgrade.an-unfinished-upgrade-transaction-already-exists', 'an unfinished upgrade transaction already exists', '已有未完成的升级事务'),
    'upgrade.no-unfinished-upgrade-transaction-is-available-to-resume': MessageTemplate('upgrade.no-unfinished-upgrade-transaction-is-available-to-resume', 'no unfinished upgrade transaction is available to resume', '没有可恢复的未完成升级事务'),
    'upgrade.unfinished-upgrade-transaction-targets-another-release': MessageTemplate('upgrade.unfinished-upgrade-transaction-targets-another-release', 'unfinished upgrade transaction targets another release', '未完成升级事务指向另一个 release'),
    'upgrade.unfinished-upgrade-transaction-plan-no-longer-matches': MessageTemplate('upgrade.unfinished-upgrade-transaction-plan-no-longer-matches', 'unfinished upgrade transaction plan no longer matches', '未完成升级事务的计划已不匹配'),
    'upgrade.unfinished-upgrade-transaction-still-belongs-to-a-live-process': MessageTemplate('upgrade.unfinished-upgrade-transaction-still-belongs-to-a-live-process', 'unfinished upgrade transaction still belongs to a live process', '未完成升级事务仍属于存活进程'),
    'upgrade.upgrade-transaction-identity-mismatch': MessageTemplate('upgrade.upgrade-transaction-identity-mismatch', 'upgrade transaction identity mismatch', '升级事务身份不匹配'),
    'upgrade.upgrade-transaction-changed-concurrently': MessageTemplate('upgrade.upgrade-transaction-changed-concurrently', 'upgrade transaction changed concurrently', '升级事务发生并发变更'),
    'upgrade.shell-profile-contains-duplicate-or-damaged-tenetora-path-markers': MessageTemplate('upgrade.shell-profile-contains-duplicate-or-damaged-tenetora-path-markers', 'shell profile contains duplicate or damaged Tenetora PATH markers', 'Shell profile 含重复或损坏的 Tenetora PATH 标记'),
    'upgrade.shell-profile-contains-a-damaged-tenetora-path-marker': MessageTemplate('upgrade.shell-profile-contains-a-damaged-tenetora-path-marker', 'shell profile contains a damaged Tenetora PATH marker', 'Shell profile 含损坏的 Tenetora PATH 标记'),
    'upgrade.refusing-to-update-a-symbolic-link-shell-profile': MessageTemplate('upgrade.refusing-to-update-a-symbolic-link-shell-profile', 'refusing to update a symbolic-link shell profile', '拒绝更新符号链接形式的 Shell profile'),
    'upgrade.shell-profile-is-not-a-regular-file': MessageTemplate('upgrade.shell-profile-is-not-a-regular-file', 'shell profile is not a regular file', 'Shell profile 不是普通文件'),
    'upgrade.shell-profile-is-not-owned-by-the-current-user': MessageTemplate('upgrade.shell-profile-is-not-owned-by-the-current-user', 'shell profile is not owned by the current user', 'Shell profile 不属于当前用户'),
    'upgrade.shell-profile-is-writable-by-group-or-others': MessageTemplate('upgrade.shell-profile-is-writable-by-group-or-others', 'shell profile is writable by group or others', 'Shell profile 可被用户组或其他用户写入'),
}
_LEGACY_MESSAGE_TEMPLATES_BY_ENGLISH = {item.english: item for item in MESSAGE_TEMPLATES.values()}
MESSAGE_TEMPLATES.update({
    code: MessageTemplate(code, english, chinese)
    for code, english, chinese in (
        ('upgrade.release-manifest-missing-required-fields', 'release manifest is missing required fields: {fields}', '发布清单缺少必需字段：{fields}'),
        ('upgrade.release-manifest-invalid', 'release manifest is invalid: {detail}', '发布清单无效：{detail}'),
        ('upgrade.embedded-release-manifest-invalid', 'embedded release manifest is invalid: {detail}', 'ZIP 内嵌发布清单无效：{detail}'),
        ('upgrade.managed-release-manifest-invalid', 'managed release manifest is invalid: {detail}', '受管 release 清单无效：{detail}'),
        ('upgrade.release-source-environment-options-are-mutually-exclusive', 'release source environment options are mutually exclusive: {detail}', '发布源环境选项互斥：{detail}'),
        ('upgrade.bridge-minimum-not-met', 'installed Tenetora {installed} is older than the direct bridge minimum {minimum}', '已安装的 Tenetora {installed} 低于直接升级最低版本 {minimum}'),
        ('upgrade.managed-pointer-unowned', 'refusing to replace unowned pointer: {name}', '拒绝替换无归属指针：{name}'),
        ('upgrade.managed-pointer-restore-failed', 'managed pointer replacement failed and its backup could not be restored: {name}', '受管指针替换失败且备份无法恢复：{name}'),
        ('upgrade.cannot-create-the-managed-windows-release-junction', 'cannot create the managed Windows release junction{detail}', '无法创建受管 Windows release 目录联接{detail}'),
        ('upgrade.worker-stage-failed', 'upgrade stage failed: {stage}', '升级阶段失败：{stage}'),
    )
})
MESSAGE_TEMPLATES_BY_ENGLISH = {**_LEGACY_MESSAGE_TEMPLATES_BY_ENGLISH,
    **{item.english: item for item in MESSAGE_TEMPLATES.values()}}


def message_issue(value: object) -> MessageIssue | None:
    if isinstance(value, MessageIssue):
        return value
    if isinstance(value, BaseException) and value.args and isinstance(value.args[0], MessageIssue):
        return value.args[0]
    if isinstance(value, dict):
        code = value.get('code', value.get('message_code'))
        fallback = value.get('message', '')
        parameters = value.get('parameters', value.get('message_parameters', {}))
        if isinstance(code, str) and code.startswith('upgrade.') and isinstance(fallback, str) and isinstance(parameters, dict):
            return MessageIssue(code, fallback, parameters=parameters)
    return None


def issue_code(issue: object) -> str:
    coded = message_issue(issue)
    if coded is not None:
        return coded.code
    template = MESSAGE_TEMPLATES_BY_ENGLISH.get(str(issue))
    if template:
        return template.code
    text = str(issue)
    prefixes = {
        "release manifest is missing required fields:": "upgrade.release-manifest-missing-required-fields",
        "release manifest is invalid:": "upgrade.release-manifest-invalid",
        "embedded release manifest is invalid:": "upgrade.embedded-release-manifest-invalid",
        "managed release manifest is invalid:": "upgrade.managed-release-manifest-invalid",
        "release source environment options are mutually exclusive:": "upgrade.release-source-environment-options-are-mutually-exclusive",
    }
    for prefix, code in prefixes.items():
        if text.startswith(prefix):
            return code
    return "upgrade.unmapped"
