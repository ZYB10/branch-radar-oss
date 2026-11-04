from __future__ import annotations

import html
import json
import re
import shutil
from collections.abc import Iterable
from typing import Any, ClassVar

from ..config import LarkSettings
from ..process import CommandRunner, ProcessResult
from ..timeutil import display_local_time


class LarkError(RuntimeError):
    def __init__(self, message: str, *, may_have_succeeded: bool = False):
        super().__init__(message)
        self.may_have_succeeded = may_have_succeeded


MAX_LARK_STDOUT_BYTES = 4_194_304
MAX_LARK_STDERR_BYTES = 262_144


class LarkCli:
    NOTICE_ENV: ClassVar[dict[str, str]] = {
        "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
        "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
    }

    def __init__(
        self,
        settings: LarkSettings,
        runner: CommandRunner | None = None,
    ):
        self.settings = settings
        self.runner = runner or CommandRunner()
        self.binary = settings.binary or shutil.which("lark-cli")
        self._status_cache: dict[str, Any] | None = None

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        if self._status_cache is not None and not refresh:
            return dict(self._status_cache)
        if not self.binary:
            self._status_cache = {"available": False, "verified": False, "error": "未找到 lark-cli"}
            return dict(self._status_cache)
        version_result = self._run([self.binary, "--version"], timeout=15)
        auth_result = self._run(
            [self.binary, "auth", "status", "--json", "--verify"], timeout=30
        )
        if not auth_result.ok:
            self._status_cache = {
                "available": version_result.ok,
                "binary": self.binary,
                "version": (version_result.stdout or version_result.stderr).strip(),
                "verified": False,
                "error": _friendly_lark_error(auth_result),
            }
            return dict(self._status_cache)
        try:
            raw = _load_json(auth_result.stdout)
        except LarkError as exc:
            self._status_cache = {
                "available": version_result.ok,
                "binary": self.binary,
                "version": (version_result.stdout or version_result.stderr).strip(),
                "verified": False,
                "error": str(exc),
            }
            return dict(self._status_cache)
        identities = raw.get("identities") or {}
        user = identities.get("user") or {}
        user_verified = bool(
            user
            and raw.get("verified") is True
            and raw.get("identity") == "user"
            and user.get("available")
            and user.get("status") in {"ready", "needs_refresh"}
            and user.get("tokenStatus") in {"valid", "needs_refresh"}
        )
        self._status_cache = {
            "available": True,
            "binary": self.binary,
            "version": (version_result.stdout or version_result.stderr).strip(),
            "identity": raw.get("identity"),
            "verified": user_verified,
            "user_name": user.get("userName"),
            "token_status": user.get("tokenStatus"),
            "scopes": user.get("scope") or [],
            "error": None if user_verified else (
                user.get("message")
                or "飞书用户身份未就绪（tokenStatus=%s）；请运行 lark-cli auth login 重新授权。"
                % (user.get("tokenStatus") or "missing")
            ),
        }
        return dict(self._status_cache)

    def check_document_scopes(self, *, create: bool = False) -> dict[str, Any]:
        binary = self._require_binary()
        required = ["docx:document:readonly", "docx:document:write_only"]
        if create:
            required.append("docx:document:create")
        scopes = " ".join(required)
        result = self._run(
            [binary, "auth", "check", "--scope", scopes, "--json"], timeout=30
        )
        raw = None
        if result.stdout.strip():
            try:
                raw = _load_json(result.stdout)
            except LarkError:
                raw = None
        if raw and raw.get("missing"):
            message = "飞书文档权限不足，缺少：%s" % ", ".join(raw["missing"])
            if raw.get("suggestion"):
                message += "；%s" % raw["suggestion"]
            raise LarkError(message)
        if not result.ok:
            raise LarkError(_friendly_lark_error(result))
        if raw is None:
            raw = _load_json(result.stdout)
        if raw.get("ok") is not True:
            raise LarkError(_envelope_error(raw, "飞书文档权限检查失败"))
        return raw

    def verify_document(self, document: str) -> dict[str, Any]:
        binary = self._require_ready()
        scopes = self.check_document_scopes()
        missing = scopes.get("missing")
        if missing:
            raise LarkError("飞书文档权限不足：%s" % missing)
        result = self._run(
            [
                binary, "docs", "+fetch", "--doc", document,
                "--scope", "outline", "--max-depth", "1",
                "--detail", "simple", "--doc-format", "xml",
                "--as", "user", "--format", "json",
            ],
            timeout=self.settings.timeout_seconds,
        )
        envelope = self._require_envelope(result, "验证飞书文档")
        doc = (envelope.get("data") or {}).get("document") or {}
        return {
            "document_id": doc.get("document_id"),
            "revision_id": doc.get("revision_id"),
            "title": doc.get("title") or _document_title(doc.get("content")),
            "url": doc.get("url"),
        }

    def create_document(self, title: str) -> dict[str, Any]:
        binary = self._require_ready()
        scopes = self.check_document_scopes(create=True)
        if scopes.get("missing"):
            raise LarkError("创建飞书文档所需权限不足：%s" % scopes["missing"])
        content = (
            "<title>%s</title>"
            "<callout emoji=\"📡\" background-color=\"light-blue\" border-color=\"blue\">"
            "<p><b>Branch Radar 项目动态</b></p>"
            "<p>这份文档由本地 Branch Radar 维护。Git 事实来自被关注分支；AI 摘要仅用于辅助理解。</p>"
            "</callout>"
            "<h1>变更日志</h1>"
            "<p>首次初始化只建立基线，后续变化会追加到这里。</p>"
        ) % html.escape(title)
        result = self._run(
            [
                binary, "docs", "+create", "--as", "user",
                "--doc-format", "xml", "--content", "-", "--format", "json",
            ],
            stdin=content,
            timeout=self.settings.timeout_seconds,
        )
        envelope = self._require_envelope(result, "创建飞书文档", mutation=True)
        document = (envelope.get("data") or {}).get("document") or {}
        token = document.get("document_id")
        url = document.get("url")
        if not token:
            raise LarkError("飞书返回成功，但缺少 document_id", may_have_succeeded=True)
        warning = None
        try:
            self.verify_document(token)
        except LarkError as exc:
            warning = "文档已创建，但回查验证失败：%s" % exc
        warnings = envelope.get("warnings") or (envelope.get("data") or {}).get("warnings") or []
        return {"token": token, "url": url or token, "warning": warning, "warnings": warnings}

    def contains_marker(self, document: str, marker: str) -> bool:
        binary = self._require_ready()
        result = self._run(
            [
                binary, "docs", "+fetch", "--doc", document,
                "--scope", "keyword", "--keyword", marker,
                "--detail", "simple", "--doc-format", "xml",
                "--as", "user", "--format", "json",
            ],
            timeout=self.settings.timeout_seconds,
        )
        if not result.ok:
            # A keyword miss may be represented as an empty success response, not an error.
            raise LarkError(_friendly_lark_error(result))
        envelope = _load_json(result.stdout)
        if envelope.get("ok") is not True:
            raise LarkError(_envelope_error(envelope, "读取飞书文档失败"))
        content = (((envelope.get("data") or {}).get("document") or {}).get("content") or "")
        return marker in content

    def append_xml(self, document: str, content: str) -> dict[str, Any]:
        binary = self._require_ready()
        result = self._run(
            [
                binary, "docs", "+update", "--doc", document,
                "--command", "append", "--doc-format", "xml",
                "--content", "-", "--as", "user", "--format", "json",
            ],
            stdin=content,
            timeout=self.settings.timeout_seconds,
        )
        return self._require_envelope(result, "更新飞书文档", mutation=True)

    @staticmethod
    def warnings(envelope: dict[str, Any]) -> list[Any]:
        return list(
            envelope.get("warnings")
            or (envelope.get("data") or {}).get("warnings")
            or []
        )

    def _require_binary(self) -> str:
        binary = self.binary
        if not binary:
            raise LarkError("未找到 lark-cli")
        return binary

    def _require_ready(self) -> str:
        status = self.status()
        if not status.get("verified"):
            raise LarkError(status.get("error") or "lark-cli 用户身份尚未就绪")
        return self._require_binary()

    def _require_envelope(
        self, result: ProcessResult, action: str, *, mutation: bool = False
    ) -> dict[str, Any]:
        if not result.ok:
            raise LarkError(
                _friendly_lark_error(result),
                may_have_succeeded=mutation,
            )
        try:
            raw = _load_json(result.stdout)
        except LarkError as exc:
            raise LarkError(
                str(exc),
                may_have_succeeded=mutation,
            ) from exc
        if raw.get("ok") is not True:
            raise LarkError(_envelope_error(raw, action + "失败"))
        data = raw.get("data") or {}
        operation_result = data.get("result")
        if operation_result == "partial_success":
            raise LarkError(action + "仅部分成功，需要回查后重试", may_have_succeeded=True)
        if operation_result == "failed":
            raise LarkError(action + "失败")
        return raw

    def _run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float,
        stdout_limit_bytes: int = MAX_LARK_STDOUT_BYTES,
    ) -> ProcessResult:
        """Bound lark-cli output: a large document body must not become memory."""
        return self.runner.run_limited(
            argv,
            stdin=stdin,
            timeout=timeout,
            env=self.NOTICE_ENV,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=MAX_LARK_STDERR_BYTES,
        )


def render_event_xml(event: dict[str, Any]) -> str:
    facts = event["facts"]
    summary = event.get("summary") or {}
    marker = "BRANCH-RADAR:%s" % event["event_key"]
    diff = facts.get("diff") or {}
    files = diff.get("files") or []
    commits = facts.get("commits") or []
    non_merge = [commit for commit in commits if len(commit.get("parents") or []) <= 1]
    contributors = _unique([commit.get("author_name") for commit in non_merge or commits])
    complete = facts.get("analysis_complete", True) is not False
    commit_count = facts.get("commit_count")
    file_count = diff.get("files_changed")
    if commit_count is None and complete and not facts.get("commits_are_tip_sample"):
        commit_count = len(commits)
    if file_count is None and complete:
        file_count = len(files)

    title = summary.get("title") or _event_label(event.get("kind"))
    metrics = [
        "检测于 %s" % display_local_time(event.get("detected_at") or ""),
        "%s 个提交" % commit_count if commit_count is not None else "提交数未知",
        "%s 个文件" % file_count if file_count is not None else "文件数未知",
        "+%s / -%s" % (
            diff.get("insertions", 0) if complete else "?",
            diff.get("deletions", 0) if complete else "?",
        ),
        "贡献者：%s" % ("、".join(contributors) or "未识别"),
    ]

    parts = [
        "<hr/>",
        "<h2>%s / %s · %s</h2>" % (
            _x(event["repository"]), _x(event["branch"]), _x(title)
        ),
        "<p><span text-color=\"gray\">%s</span></p>" % _x(" · ".join(metrics)),
        "<p>%s</p>" % _x(summary.get("overview") or "分支发生变化"),
    ]

    highlights = (summary.get("highlights") or [])[:3]
    if highlights:
        parts.append("<h3>主要变化</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _x(item) for item in highlights))

    risks = _visible_risks(event.get("kind"), summary)
    if risks:
        parts.append("<h3>需要关注</h3><ul>%s</ul>" % "".join(
            "<li><b>%s · %s</b>：%s</li>" % (
                _x({"medium": "中风险", "high": "高风险"}[item["level"]]),
                _x(item["area"]),
                _x(item["reason"]),
            )
            for item in risks
        ))

    verification = (summary.get("verification") or [])[:2]
    if verification:
        parts.append("<h3>建议验证</h3><ul>%s</ul>" % "".join("<li>%s</li>" % _x(item) for item in verification))

    evidence = "证据：SHA %s → %s" % (
        _short(facts.get("old_sha") or facts.get("previous_tip")),
        _short(facts.get("new_sha")),
    )
    if files:
        evidence += " · 文件：%s" % _x("、".join(files[:3]))
    parts.append(
        "<p><span text-color=\"gray\">%s · %s</span></p>"
        % (evidence, _x(marker))
    )
    return "".join(parts)


def _visible_risks(kind: str | None, summary: dict[str, Any]) -> list[dict[str, str]]:
    visible: list[dict[str, str]] = []
    for item in summary.get("risks") or []:
        if not isinstance(item, dict):
            continue
        level = str(item.get("level") or "").lower()
        if level not in {"medium", "high"}:
            continue
        visible.append({
            "level": level,
            "area": str(item.get("area") or "风险"),
            "reason": str(item.get("reason") or "需要人工确认。"),
        })

    abnormal = {
        "force_push": ("high", "Git 历史", "检测到强制推送，已有提交可能被替换或移除。"),
        "rewind": ("high", "Git 历史", "检测到分支回退，原有提交可能不再位于当前分支。"),
        "deleted": ("high", "分支状态", "远程分支已不可见，请确认是否为预期删除。"),
        "recreated": ("medium", "分支状态", "曾删除的分支重新出现，请确认新的历史起点。"),
        "history_gap": ("high", "分析完整性", "缺少旧提交对象，无法完整还原本次变化。"),
    }.get(kind or "")
    if abnormal and not any(item["area"] == abnormal[1] for item in visible):
        visible.insert(0, {
            "level": abnormal[0], "area": abnormal[1], "reason": abnormal[2]
        })

    deployment = summary.get("deployment") or {}
    impact = str(deployment.get("impact") or "none").lower()
    if impact in {"possible", "required", "unknown"} and not any(
        "部署" in item["area"] for item in visible
    ):
        notes = _unique(deployment.get("notes") or [])
        fallback = {
            "possible": "变更可能需要同步部署或环境配置。",
            "required": "检测到需要部署配合的变更。",
            "unknown": "部署影响尚不明确，需要人工确认。",
        }[impact]
        visible.append({
            "level": "high" if impact == "required" else "medium",
            "area": "部署影响",
            "reason": "；".join(notes) or fallback,
        })
    return visible


def _event_label(kind: str | None) -> str:
    return {
        "fast_forward": "新增提交",
        "force_push": "强制推送",
        "rewind": "分支回退",
        "deleted": "分支删除",
        "recreated": "分支重新出现",
        "history_gap": "分支变化（历史不完整）",
        "baseline": "建立基线",
    }.get(kind or "", kind or "分支变化")


def _document_title(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    match = re.search(
        r"<title(?:\s[^>]*)?>(.*?)</title>", value[:65_536], re.IGNORECASE | re.DOTALL
    )
    if not match:
        return None
    without_tags = re.sub(r"<[^>]+>", "", match.group(1))
    normalized = " ".join(html.unescape(without_tags).split())
    return normalized[:500] or None


def _load_json(value: str) -> dict[str, Any]:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LarkError("lark-cli 没有返回有效 JSON：%s" % exc) from exc
    if not isinstance(raw, dict):
        raise LarkError("lark-cli JSON 顶层不是 object")
    return raw


def _friendly_lark_error(result: ProcessResult) -> str:
    if result.timed_out:
        return "lark-cli 执行超时"
    if result.stdout_truncated:
        return "lark-cli 输出超过 %d 字节安全上限，已中止读取" % MAX_LARK_STDOUT_BYTES
    detail = (result.stderr or result.stdout).strip()
    if "keychain" in detail.lower():
        return "当前进程无法访问 macOS Keychain；请在普通终端运行，或检查后台任务的钥匙串权限。不会自动降低凭证存储安全级别。"
    try:
        raw = json.loads(detail)
        if isinstance(raw, dict):
            return _envelope_error(raw, "lark-cli 执行失败")
    except json.JSONDecodeError:
        pass
    return detail[-2000:] if detail else "lark-cli 执行失败（exit %s）" % result.returncode


def _envelope_error(raw: dict[str, Any], fallback: str) -> str:
    error = raw.get("error") or {}
    if isinstance(error, dict):
        return error.get("message") or error.get("hint") or fallback
    return fallback


def _x(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _short(value: str | None) -> str:
    return _x(value[:8] if value else "—")


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result
