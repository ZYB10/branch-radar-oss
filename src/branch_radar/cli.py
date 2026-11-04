from __future__ import annotations

import argparse
import json
import shutil
import signal
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any

from . import __version__
from .config import ConfigStore
from .database import Database
from .git import GitClient, GitError
from .i18n import SUPPORTED_LOCALES, set_locale, t
from .integrations.codex_cli import CodexCli
from .integrations.lark_cli import LarkCli, LarkError
from .locking import LockUnavailable, exclusive_lock
from .models import RepositoryCandidate
from .notifications import DesktopNotifier
from .paths import AppPaths
from .retention import parse_retention_cutoff
from .scanner import RepositoryScanner
from .security import redact_sensitive_text
from .service import ServiceInstallError, ServiceManager, ServiceSpec, build_manager
from .services import RadarService, ServiceError, validate_scan_roots
from .timeutil import display_local_time
from .ui import Console, parse_selection


def _format_bytes(value: int | None) -> str:
    if not isinstance(value, int) or value < 0:
        return t("未知")
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.0f %s" % (size, unit) if unit == "B" else "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f GB" % size


def _daemon_retry_delay(interval: int, consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return interval
    multiplier = 1 << min(consecutive_failures - 1, 8)
    return min(
        interval * multiplier,
        max(interval, 3600),
    )


class Application:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.paths = AppPaths.discover()
        self.paths.ensure()
        self.store = ConfigStore(self.paths)
        self.config = self.store.load()
        self.database = Database(self.paths.database)
        self.database.initialize()
        self.git = GitClient()
        self.console = Console(color=False if getattr(args, "no_color", False) else None)

    @property
    def json_output(self) -> bool:
        return bool(getattr(self.args, "json", False))

    def codex(self) -> CodexCli:
        return CodexCli(self.config.codex, self.paths)

    def lark(self) -> LarkCli:
        return LarkCli(self.config.lark)

    def emit(self, payload: Any) -> None:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def command_doctor(self) -> int:
        git_binary = shutil.which("git")
        git_version = None
        if git_binary:
            result = self.git.runner.run([git_binary, "--version"], timeout=10)
            git_version = result.stdout.strip() if result.ok else None
        codex = self.codex().status()
        lark = self.lark().status()
        daemon_health = self.database.daemon_health()
        payload: dict[str, Any] = {
            "ok": bool(git_binary),
            "version": __version__,
            "home": str(self.paths.home),
            "config": str(self.paths.config),
            "database": str(self.paths.database),
            "git": {"available": bool(git_binary), "binary": git_binary, "version": git_version},
            "codex": codex,
            "lark": lark,
            "watched_branches": len(self.database.list_watches()),
            "daemon_health": daemon_health,
            "service": self._service_probe(),
            "storage": self._storage_probe(),
        }
        if self.json_output:
            self.emit(payload)
        else:
            self.console.header("Branch Radar 诊断", "确认本地 Git、Codex 与飞书连接")
            if git_binary:
                self.console.success(t("Git 可用 · %s") % (git_version or git_binary))
            else:
                self.console.error("未找到 Git")
            if codex.get("available") and codex.get("authenticated"):
                self.console.success(t("Codex CLI 可用并已登录 · %s") % (codex.get("version") or ""))
            elif codex.get("available"):
                self.console.warning("Codex CLI 可用，但登录状态不可用")
                self.console.muted(str(codex.get("error") or ""))
            else:
                self.console.warning("未连接 Codex CLI；规则摘要仍可工作")
            if lark.get("verified"):
                identity = lark.get("user_name") or "user"
                self.console.success(t("lark-cli 已连接 · %s") % identity)
            elif lark.get("available"):
                self.console.warning("lark-cli 已安装，但当前进程无法验证用户身份")
                self.console.muted(str(lark.get("error") or ""))
            else:
                self.console.warning("未连接 lark-cli；本地监控仍可工作")
            service = payload["service"]
            if service.get("loaded"):
                self.console.success(
                    t("后台服务运行中 · %s") % (service.get("unit") or service.get("platform"))
                )
            elif service.get("installed"):
                self.console.warning(t("后台服务已安装但未运行：%s") % service.get("unit"))
            else:
                self.console.muted(
                    "未安装后台服务；关闭终端即停止检查（branch-radar service install）"
                )
            failures = int(daemon_health.get("consecutive_failures") or 0)
            last_success_at = daemon_health.get("last_success_at")
            last_success_label = (
                display_local_time(last_success_at)
                if isinstance(last_success_at, str)
                else t("从未")
            )
            if failures:
                self.console.warning(
                    t("检查任务连续失败 %d 次；上次成功：%s")
                    % (failures, last_success_label)
                )
                if daemon_health.get("last_error"):
                    self.console.muted(str(daemon_health["last_error"]))
            elif isinstance(last_success_at, str):
                self.console.success(
                    t("上次成功检查：%s") % display_local_time(last_success_at)
                )
            else:
                self.console.muted("尚无成功检查记录")
            self.console.info(t("状态目录：%s") % self.paths.home)
            self.console.info(t("已关注 %d 个分支") % payload["watched_branches"])
            storage = payload["storage"]
            self.console.info(
                t("数据库 %s · %d 条事件")
                % (_format_bytes(storage.get("database_bytes")), storage.get("events") or 0)
            )
            if storage.get("retention_days"):
                last_prune_at = daemon_health.get("last_prune_at")
                self.console.muted(
                    t("保留 %d 天；上次清理：%s")
                    % (
                        storage["retention_days"],
                        display_local_time(last_prune_at)
                        if isinstance(last_prune_at, str)
                        else t("从未"),
                    )
                )
            else:
                self.console.muted("未设置保留期限；历史将持续增长（branch-radar prune）")
        return 0 if payload["ok"] else 1

    def discover(self, roots: Sequence[str] | None = None) -> list[RepositoryCandidate]:
        root_values = validate_scan_roots(
            self.config.scan_roots if roots is None else roots
        )
        if not root_values:
            return []
        scanner = RepositoryScanner(self.git)
        return scanner.scan(
            [Path(value).expanduser() for value in root_values],
            max_depth=getattr(self.args, "max_depth", None) or self.config.scan_max_depth,
        )

    def command_discover(self) -> int:
        requested_roots = getattr(self.args, "paths", None)
        try:
            candidates = self.discover(requested_roots or None)
        except ServiceError as exc:
            return self._fail(str(exc))
        if self.json_output:
            self.emit({"repositories": [item.to_dict() for item in candidates]})
            return 0
        self.console.header("发现本地仓库", "自动读取 origin、当前分支与 upstream")
        if not candidates:
            self.console.warning("没有找到 Git 仓库；可用 discover PATH 指定其他目录")
            return 0
        rows = []
        for index, item in enumerate(candidates, 1):
            rows.append((
                index,
                item.name,
                item.current_branch or "detached",
                (t("不可用：%s") % item.error) if item.error else (item.remote_url or t("仅本地")),
                item.path,
            ))
        self.console.table(("#", "仓库", "当前分支", "远程", "本地路径"), rows)
        self.console.muted(t("共找到 %d 个 checkout；相同远程的多个 checkout 会分别展示。") % len(candidates))
        return 0

    def command_init(self) -> int:
        non_interactive = bool(getattr(self.args, "non_interactive", False) or self.json_output)
        requested_roots = getattr(self.args, "roots", None)
        roots = list(
            self.config.scan_roots if requested_roots is None else requested_roots
        )
        if not roots:
            if non_interactive:
                return self._fail("首次初始化需要显式指定至少一个 --root PATH")
            selected_root = self.console.ask(
                "扫描根目录",
                default=str(Path.cwd()),
            ).strip()
            if not selected_root:
                return self._fail("请至少选择一个扫描目录")
            roots = [selected_root]
        try:
            self.config.scan_roots = validate_scan_roots(roots)
        except ServiceError as exc:
            return self._fail(str(exc))
        select_all = bool(getattr(self.args, "all", False))
        if non_interactive and not select_all:
            return self._fail("非交互或 JSON 初始化需要显式添加 --all")

        if not self.json_output:
            self.console.header("欢迎使用 Branch Radar", "几步完成仓库发现、基线和本地工具连接")
            self.console.info("正在检查本地工具…")
        codex_status = (
            {"available": False, "authenticated": False, "disabled": True}
            if getattr(self.args, "no_codex", False)
            else self.codex().status()
        )
        lark_status = (
            {"available": False, "verified": False, "disabled": True}
            if getattr(self.args, "no_lark", False)
            else self.lark().status()
        )

        if not self.json_output:
            self.console.success("Git 已就绪")
            if codex_status.get("authenticated"):
                self.console.success("发现已登录的 Codex CLI")
            else:
                self.console.warning("Codex 暂不可用，将使用确定性规则摘要")
            if lark_status.get("verified"):
                self.console.success(t("发现已登录的 lark-cli · %s") % (lark_status.get("user_name") or "user"))
            else:
                self.console.warning("飞书身份暂不可用，可在普通终端稍后连接")
            self.console.info(t("正在扫描：%s") % "、".join(self.config.scan_roots))
        candidates = self.discover(self.config.scan_roots)

        if not candidates:
            if self.json_output:
                self.emit({"ok": False, "error": "没有发现 Git 仓库"})
            else:
                self.console.error("没有发现 Git 仓库")
            return 1

        if select_all or non_interactive:
            selected = list(range(len(candidates)))
        else:
            self.console.print()
            self.console.table(
                ("#", "仓库", "分支", "远程"),
                [
                    (
                        index,
                        item.name,
                        item.current_branch or "detached",
                        (t("不可用：%s") % item.error)
                        if item.error else (item.remote_url or t("仅本地")),
                    )
                    for index, item in enumerate(candidates, 1)
                ],
            )
            answer = self.console.ask(
                "选择要关注的仓库（如 1,3-5 或 all；直接回车暂不关注）"
            )
            try:
                selected = parse_selection(answer, len(candidates))
            except (ValueError, TypeError):
                self.console.error("选择格式无效")
                return 2

        self.config.codex.enabled = bool(codex_status.get("authenticated")) and not getattr(self.args, "no_codex", False)
        self.config.lark.enabled = bool(lark_status.get("verified")) and not getattr(self.args, "no_lark", False)
        service = RadarService(
            self.paths,
            self.store,
            self.config,
            self.database,
            self.git,
        )
        if not selected:
            try:
                self.config = service.update_preferences(
                    scan_roots=self.config.scan_roots,
                    codex_enabled=self.config.codex.enabled,
                    lark_enabled=self.config.lark.enabled,
                )
            except ServiceError as exc:
                return self._fail(str(exc))
            payload = {
                "ok": True,
                "watched": [],
                "skipped": [],
                "message": "本次未选择仓库",
                "home": str(self.paths.home),
            }
            if self.json_output:
                self.emit(payload)
            else:
                self.console.info("本次未选择仓库；稍后可运行 branch-radar init 或 watch add")
            return 0

        watch_entries: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for index in selected:
            candidate = candidates[index]
            if candidate.error:
                skipped.append({"path": candidate.path, "error": candidate.error})
                if not self.json_output:
                    self.console.warning(t("跳过 %s：%s") % (candidate.path, candidate.error))
                continue
            default_branch = (
                candidate.upstream_branch or candidate.current_branch or candidate.default_branch
            )
            if not default_branch:
                skipped.append({
                    "path": candidate.path,
                    "error": "无法确定要关注的分支",
                })
                if not self.json_output:
                    self.console.warning(t("跳过 %s：无法确定分支") % candidate.name)
                continue
            branch_value = default_branch
            if not non_interactive and not select_all:
                branch_value = self.console.ask(
                    t("%s 要关注的分支（多个用逗号分隔）") % candidate.name,
                    default=default_branch,
                )
            branches = [item.strip() for item in branch_value.replace("，", ",").split(",") if item.strip()]
            watch_entries.append({"path": candidate.path, "branches": branches})

        watched: list[dict[str, Any]] = []
        if watch_entries:
            try:
                watch_payload = service.add_watches(watch_entries)
            except ServiceError as exc:
                skipped.append({"path": "", "error": str(exc)})
            else:
                watched = watch_payload["watched"]
                skipped.extend(watch_payload["skipped"])
            if not self.json_output:
                for item in skipped:
                    self.console.warning(str(item.get("error") or "仓库未添加"))

        if not watched:
            payload = {
                "ok": False,
                "error": "没有成功建立任何分支关注",
                "watched": [],
                "skipped": skipped,
                "home": str(self.paths.home),
            }
            if self.json_output:
                self.emit(payload)
            else:
                self.console.error("没有成功建立任何分支关注")
            return 1

        if (
            self.config.lark.enabled
            and not non_interactive
            and not self.config.lark.document
            and self.console.confirm("启用飞书文档同步吗？", default=True)
        ):
            document = self.console.ask(
                "粘贴已有文档 URL/token；输入 new 新建；留空稍后设置"
            )
            if document.lower() in {"new", "新建", "create"}:
                title = self.console.ask("新文档标题", default=self.config.lark.document_title)
                if self.console.confirm("确认使用当前飞书用户创建文档？", default=True):
                    try:
                        created = service.create_lark_document(title)
                        self.config = service.config
                        if not self.json_output:
                            self.console.success(t("已创建飞书文档：%s") % created["url"])
                            if created.get("warning"):
                                self.console.warning(str(created["warning"]))
                            for warning in created.get("warnings") or []:
                                self.console.warning(t("飞书提示：%s") % warning)
                    except ServiceError as exc:
                        if not self.json_output:
                            self.console.warning(t("创建文档失败：%s") % exc)
            elif document:
                try:
                    service.set_lark_document(document)
                    self.config = service.config
                except ServiceError as exc:
                    if not self.json_output:
                        self.console.warning(t("文档验证失败，暂未关联：%s") % exc)

        try:
            self.config = service.update_preferences(
                scan_roots=self.config.scan_roots,
                codex_enabled=self.config.codex.enabled,
                lark_enabled=self.config.lark.enabled,
            )
        except ServiceError as exc:
            return self._fail(str(exc))
        payload = {
            "ok": True,
            "watched": watched,
            "skipped": skipped,
            "codex_enabled": self.config.codex.enabled,
            "lark_enabled": self.config.lark.enabled,
            "lark_document": self.config.lark.document,
            "home": str(self.paths.home),
        }
        if self.json_output:
            self.emit(payload)
        else:
            self.console.print()
            self.console.success(t("已关注 %d 个分支") % len(watched))
            pending_count = sum(item["baseline_pending"] for item in watched)
            ready_count = len(watched) - pending_count
            if ready_count:
                self.console.success(
                    t("%d 个初始基线已建立，不会发送历史提交") % ready_count
                )
            if pending_count:
                self.console.warning(
                    t("%d 个分支等待第一次成功刷新后静默建立基线") % pending_count
                )
            if self.config.lark.document:
                self.console.success("飞书文档已关联")
            else:
                self.console.info("稍后可用 lark set-document 或 lark create-document 关联文档")
            self.console.muted("下一步：branch-radar check")
        return 0

    def command_watch_add(self) -> int:
        service = RadarService(
            self.paths,
            self.store,
            self.config,
            self.database,
            self.git,
        )
        try:
            payload = service.add_watches([{
                "path": self.args.path,
                "branches": list(self.args.branch or []),
                "rebaseline": bool(self.args.rebaseline),
            }])
        except ServiceError as exc:
            return self._fail(str(exc))
        added = payload["watched"]
        skipped = payload["skipped"]
        if self.json_output:
            self.emit({"ok": True, "added": added, "skipped": skipped})
        else:
            for item in added:
                self.console.success(t("已关注 %s · %s") % (item["repository"], item["branch"]))
                if item["baseline_pending"]:
                    self.console.warning("远程暂不可用；第一次成功检查将静默建立基线")
                else:
                    self.console.muted(t("基线：%s") % ((item["baseline"] or "分支暂不存在")[:12]))
            for item in skipped:
                self.console.warning(str(item.get("error") or "分支未添加"))
        return 0

    def command_watch_list(self) -> int:
        watches = self.database.list_watches()
        if self.json_output:
            self.emit({"watches": watches})
            return 0
        self.console.header("关注中的分支")
        if not watches:
            self.console.warning("尚未关注分支；运行 branch-radar init")
            return 0
        self.console.table(
            ("ID", "仓库", "分支", "状态", "最新 SHA", "最近检查"),
            [
                (
                    row["branch_id"], row["repository"], row["branch"],
                    t("正常")
                if row["present"] and not row["last_error"]
                else (t("异常") if row["last_error"] else t("不存在")),
                    (row["last_observed_oid"] or "—")[:10],
                    display_local_time(row["last_checked_at"]),
                )
                for row in watches
            ],
        )
        return 0

    def command_watch_remove(self) -> int:
        service = RadarService(
            self.paths, self.store, self.config, self.database, self.git
        )
        try:
            removed = service.remove_watch(self.args.watch_id)
        except ServiceError as exc:
            return self._fail(str(exc))
        if self.json_output:
            self.emit({"ok": True, "removed": removed})
        else:
            self.console.success(
                t("已取消关注 %s · %s")
                % (removed["repository"], removed["branch"])
            )
            self.console.muted("历史事件和检查游标已保留；重新添加可继续关注")
        return 0

    def command_check(self) -> int:
        use_codex = bool(getattr(self.args, "codex", False))
        service = RadarService(
            self.paths, self.store, self.config, self.database, self.git
        )
        try:
            results, publications = service.check(
                fetch=bool(getattr(self.args, "fetch", True)),
                use_codex=use_codex,
                publish=bool(getattr(self.args, "publish", False)),
            )
            self.config = service.config
        except ServiceError as exc:
            return self._fail(str(exc))

        payload = {
            "results": [result.to_dict() for result in results],
            "publications": publications,
        }
        if self.json_output:
            self.emit(payload)
            return 1 if (
                any(result.status == "error" for result in results)
                or any(
                    publication.get("status") in {"error", "unknown"}
                    for publication in publications
                )
            ) else 0
        self.console.header("分支检查结果")
        if not results:
            self.console.warning("没有关注中的分支；运行 branch-radar init")
            return 0
        for result in results:
            label = "%s · %s" % (result.repository, result.branch)
            if result.status == "unchanged":
                self.console.info(t("%s 没有变化") % label)
            elif result.status == "baseline":
                self.console.success(t("%s 已建立基线") % label)
            elif result.status == "error":
                self.console.error("%s：%s" % (label, result.error))
            else:
                self.console.success(t("%s 检测到 %s") % (label, result.event_kind))
                if result.summary:
                    self.console.muted(result.summary.overview)
                if result.warning:
                    self.console.warning(result.warning)
        for publication in publications:
            status = publication.get("status")
            if status in {"published", "already_present"}:
                self.console.success(t("事件 #%s 已同步到飞书") % publication.get("event_id"))
            else:
                self.console.warning(t("事件 #%s 飞书同步状态：%s") % (publication.get("event_id"), status))
            for warning in publication.get("warnings") or []:
                self.console.warning(t("飞书提示：%s") % warning)
        return 1 if (
            any(result.status == "error" for result in results)
            or any(
                publication.get("status") in {"error", "unknown"}
                for publication in publications
            )
        ) else 0

    def command_status(self) -> int:
        watches = self.database.list_watches()
        events = self.database.list_events(limit=10, include_baseline=False)
        health = self.database.daemon_health()
        payload: dict[str, Any] = {
            "home": str(self.paths.home),
            "watches": watches,
            "recent_events": events,
            "daemon_health": health,
            "lark_document": self.config.lark.document,
            "codex_enabled": self.config.codex.enabled,
        }
        if self.json_output:
            self.emit(payload)
            return 0
        self.console.header("Branch Radar 状态")
        self.console.info(t("关注 %d 个分支，已记录 %d 条近期事件") % (len(watches), len(events)))
        for watch in watches:
            marker = "✓" if watch["present"] and not watch["last_error"] else "!"
            self.console.print("  %s %s · %s  %s" % (
                marker, watch["repository"], watch["branch"], (watch["last_observed_oid"] or "—")[:10]
            ))
        if self.config.lark.document:
            self.console.info(t("飞书文档：%s") % self.config.lark.document)
        last_success_at = health.get("last_success_at")
        if isinstance(last_success_at, str):
            self.console.info(
                t("上次成功检查：%s") % display_local_time(last_success_at)
            )
        return 0

    def command_prune(self) -> int:
        try:
            before = parse_retention_cutoff(self.args.before)
            with exclusive_lock(self.paths.home / "operation.lock"):
                result = self.database.prune(
                    before,
                    dry_run=bool(self.args.dry_run),
                    vacuum=bool(self.args.vacuum),
                )
        except LockUnavailable as exc:
            return self._fail(str(exc))
        except ValueError as exc:
            return self._fail(str(exc))
        if self.json_output:
            self.emit({"ok": True, **result})
            return 0
        action = t("可清理") if result["dry_run"] else t("已清理")
        self.console.header("历史数据保留")
        self.console.info(
            t("%s %d 条事件、%d 个检查批次、%d 条投递记录和 %d 条外部请求")
            % (
                action,
                result["events"],
                result["check_runs"],
                result["deliveries"],
                result["external_requests"],
            )
        )
        if result["protected_events"]:
            self.console.warning(
                t("%d 条仍有关联待确认批次或待处理投递的事件已保留")
                % result["protected_events"]
            )
        if result["dry_run"]:
            self.console.muted("这是预览；移除 --dry-run 后才会写入数据库。")
        elif result["vacuumed"]:
            self.console.success("数据库空闲页已回收")
        return 0

    def command_events(self) -> int:
        events = self.database.list_events(
            limit=self.args.limit,
            include_baseline=False,
        )
        if self.json_output:
            self.emit({"events": events})
            return 0
        self.console.header("最近分支事件")
        if not events:
            self.console.info("还没有变更事件；baseline 不会作为历史通知生成")
            return 0
        rows = []
        for event in events:
            summary = event.get("summary") or {}
            rows.append((
                event["id"], event["repository"], event["branch"], event["kind"],
                display_local_time(event["detected_at"]), summary.get("title") or "—",
            ))
        self.console.table(("ID", "仓库", "分支", "类型", "时间", "摘要"), rows)
        return 0

    def command_lark_status(self) -> int:
        payload = self.lark().status()
        payload["document"] = self.config.lark.document
        if self.json_output:
            self.emit(payload)
        elif payload.get("verified"):
            self.console.success(t("lark-cli 已连接 · %s") % (payload.get("user_name") or "user"))
            self.console.info(t("文档：%s") % (self.config.lark.document or "尚未关联"))
        else:
            self.console.warning(str(payload.get("error") or "lark-cli 未就绪"))
        return 0 if payload.get("verified") else 1

    def command_lark_set_document(self) -> int:
        if self.args.skip_verify:
            try:
                with exclusive_lock(self.paths.home / "operation.lock"):
                    latest = self.store.load()
                    latest.lark.document = self.args.document
                    latest.lark.enabled = True
                    self.store.save(latest)
                    self.config = latest
            except LockUnavailable as exc:
                return self._fail(str(exc))
        else:
            service = RadarService(
                self.paths, self.store, self.config, self.database, self.git
            )
            try:
                service.set_lark_document(self.args.document)
                self.config = service.config
            except ServiceError as exc:
                return self._fail(t("无法验证文档读写条件：%s") % exc)
        if self.json_output:
            self.emit({"ok": True, "document": self.args.document})
        else:
            self.console.success(t("已关联飞书文档：%s") % self.args.document)
        return 0

    def command_lark_create_document(self) -> int:
        service = RadarService(
            self.paths, self.store, self.config, self.database, self.git
        )
        try:
            created = service.create_lark_document(
                self.args.title or self.config.lark.document_title
            )
            self.config = service.config
        except ServiceError as exc:
            return self._fail(str(exc))
        if self.json_output:
            self.emit({"ok": True, "document": created})
        else:
            self.console.success(t("已创建并关联飞书文档：%s") % created["url"])
            if created.get("warning"):
                self.console.warning(str(created["warning"]))
            for warning in created.get("warnings") or []:
                self.console.warning(t("飞书提示：%s") % warning)
        return 0

    def command_lark_publish(self) -> int:
        service = RadarService(
            self.paths, self.store, self.config, self.database, self.git
        )
        try:
            outcomes = service.publish(limit=self.args.limit)
            self.config = service.config
        except ServiceError as exc:
            return self._fail(str(exc))
        if self.json_output:
            self.emit({"outcomes": outcomes})
        else:
            if not outcomes:
                self.console.info("没有待发布事件")
            for item in outcomes:
                if item["status"] in {"published", "already_present"}:
                    self.console.success(t("事件 #%s：%s") % (item["event_id"], item["status"]))
                else:
                    self.console.warning(t("事件 #%s：%s · %s") % (item["event_id"], item["status"], item.get("error", "")))
                for warning in item.get("warnings") or []:
                    self.console.warning(t("飞书提示：%s") % warning)
        return 1 if any(item["status"] in {"error", "unknown"} for item in outcomes) else 0

    def command_codex_status(self) -> int:
        payload = self.codex().status()
        if self.json_output:
            self.emit(payload)
        elif payload.get("available") and payload.get("authenticated"):
            self.console.success(t("Codex CLI 可用并已登录 · %s") % payload.get("version"))
        else:
            self.console.warning(str(payload.get("error") or "Codex CLI 不可用"))
        return 0 if payload.get("authenticated") else 1

    def command_daemon(self) -> int:
        interval = self.args.interval or self.config.poll_interval_seconds
        if interval < 10:
            return self._fail("轮询间隔不能小于 10 秒")
        if self.args.publish and not self.config.lark.document:
            return self._fail("daemon --publish 需要先关联飞书文档")
        stopping = {"value": False}
        had_error = False
        crash_failures = 0

        def stop(_signum: int, _frame: FrameType | None) -> None:
            stopping["value"] = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        if not self.json_output:
            self.console.header("Branch Radar 正在运行", t("每 %d 秒检查一次，Ctrl-C 可安全退出") % interval)
        service = RadarService(
            self.paths, self.store, self.config, self.database, self.git
        )
        while not stopping["value"]:
            use_codex = bool(self.args.codex)
            notification = {"status": "skipped", "detail": None}
            try:
                with self.database.daemon_heartbeat():
                    results, outcomes = service.check(
                        fetch=True,
                        use_codex=use_codex,
                        publish=bool(self.args.publish),
                    )
                self.config = service.config
                crash_failures = 0
                # Housekeeping runs after the check so a retention failure can
                # never cost the cycle its Git results.
                try:
                    retention = service.enforce_retention()
                except Exception as retention_error:
                    retention = {
                        "ran": False,
                        "reason": "error",
                        "error": redact_sensitive_text(str(retention_error))[:500],
                    }
                try:
                    notification = DesktopNotifier().notify_check_run(
                        results,
                        service.last_check_run(),
                        service.config.notifications,
                    ).to_dict()
                except Exception as notification_error:
                    # A desktop alert is presentation-only. Durable events and
                    # the daemon heartbeat must survive a platform notifier bug.
                    notification = {
                        "status": "error",
                        "detail": redact_sensitive_text(
                            str(notification_error) or t("系统通知失败")
                        )[:300],
                    }
            except Exception as exc:
                crash_failures += 1
                results = []
                retention = {"ran": False, "reason": "skipped"}
                outcomes = [{
                    "status": "error",
                    "error": redact_sensitive_text(
                        str(exc) or t("后台检查失败")
                    )[:2000],
                }]
            iteration_failed = (
                any(item.status == "error" for item in results)
                or any(item.get("status") in {"error", "unknown"} for item in outcomes)
            )
            had_error = had_error or iteration_failed
            if self.json_output:
                try:
                    daemon_health = self.database.daemon_health()
                except Exception:
                    daemon_health = {
                        "last_attempt_at": None,
                        "last_success_at": None,
                        "last_failure_at": None,
                        "consecutive_failures": crash_failures,
                    }
                print(json.dumps({
                    "checked_at": time.time(),
                    "results": [item.to_dict() for item in results],
                    "publications": outcomes,
                    "daemon_health": daemon_health,
                    "retention": retention,
                    "notification": notification,
                }, ensure_ascii=False, default=str), flush=True)
            else:
                changed = sum(item.status == "changed" for item in results)
                errors = sum(item.status == "error" for item in results)
                delivery_errors = sum(
                    item.get("status") in {"error", "unknown"} for item in outcomes
                )
                self.console.info(
                    t("本轮完成：%d 个变化，%d 个分支错误，%d 个发布错误")
                    % (changed, errors, delivery_errors)
                )
                for outcome in outcomes:
                    if outcome.get("status") in {"error", "unknown"}:
                        self.console.warning(str(outcome.get("error") or outcome["status"]))
                if notification.get("status") == "sent":
                    self.console.success(t("已发送本轮变化通知"))
                elif notification.get("status") == "error":
                    self.console.warning(
                        t("系统通知失败：%s")
                        % (notification.get("detail") or t("未知错误"))
                    )
                if retention.get("ran") and retention.get("events"):
                    self.console.info(
                        t("已清理 %d 天前的 %d 条历史事件")
                        % (
                            int(retention.get("retention_days") or 0),
                            int(retention.get("events") or 0),
                        )
                    )
            if self.args.once:
                return 1 if iteration_failed else 0
            delay = interval
            if crash_failures:
                delay = _daemon_retry_delay(interval, crash_failures)
                if not self.json_output:
                    self.console.warning(
                        t("后台检查将在 %d 秒后重试（连续失败 %d 次）")
                        % (delay, crash_failures)
                    )
            deadline = time.monotonic() + delay
            while not stopping["value"] and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        return 1 if had_error else 0

    def command_web(self) -> int:
        from .web import serve_web

        service = RadarService(
            self.paths,
            self.store,
            self.config,
            self.database,
            self.git,
        )
        return serve_web(
            port=self.args.port,
            open_browser=bool(self.args.open_browser),
            service=service,
        )

    def _fail(self, message: str) -> int:
        if self.json_output:
            self.emit({"ok": False, "error": message})
        else:
            self.console.error(message)
        return 1


    def _storage_probe(self) -> dict[str, Any]:
        """Report how much the state directory holds; never fail `doctor` on it."""
        payload: dict[str, Any] = {
            "database_bytes": None,
            "events": None,
            "retention_days": self.config.retention_days,
        }
        try:
            payload["database_bytes"] = self.paths.database.stat().st_size
        except OSError:
            pass
        try:
            payload["events"] = self.database.overview_counts().get("events")
        except Exception:
            pass
        return payload

    def _service_probe(self) -> dict[str, Any]:
        """Service state is diagnostic only; never let it fail `doctor`."""
        try:
            return build_manager(self.paths, ServiceSpec()).status()
        except (ServiceInstallError, OSError) as exc:
            return {"installed": False, "loaded": False, "error": str(exc)}

    def _service_manager(self) -> ServiceManager:
        mode = getattr(self.args, "mode", "daemon")
        interval = getattr(self.args, "interval", None)
        if mode == "daemon" and interval is None:
            interval = self.config.poll_interval_seconds
        return build_manager(
            self.paths,
            ServiceSpec(
                mode=mode,
                interval=interval,
                use_codex=bool(getattr(self.args, "codex", False)),
                publish=bool(getattr(self.args, "publish", False)),
                port=int(getattr(self.args, "port", 8765) or 8765),
                label=getattr(self.args, "label", None),
            ),
        )

    def command_service_install(self) -> int:
        manager = self._service_manager()
        spec = manager.spec
        if spec.mode == "daemon" and spec.publish and not self.config.lark.document:
            return self._fail("后台自动发布需要先关联飞书文档：branch-radar lark set-document")
        if getattr(self.args, "dry_run", False):
            definitions = manager.unit_definitions()
            if self.json_output:
                self.emit({
                    "ok": True,
                    "dry_run": True,
                    "unit": str(manager.unit_path),
                    "content": manager.render(),
                    "units": [
                        {"unit": str(path), "content": content}
                        for path, content in definitions.items()
                    ],
                })
            else:
                for path, content in definitions.items():
                    self.console.header(t("将写入 %s") % path)
                    self.console.print(content)
            return 0

        result = manager.install()
        if self.json_output:
            self.emit({"ok": True, **result})
            return 0
        self.console.success(t("后台服务已安装并启动：%s") % result["unit"])
        self.console.info(t("日志：%s") % manager.stdout_log)
        if result.get("note"):
            self.console.warning(str(result["note"]))
        if spec.mode == "web":
            self.console.warning(
                "控制台在后台运行时，安全链接只会写入日志；请从日志末尾读取带 #session= 的地址。"
            )
        if spec.publish:
            self.console.warning(
                "已开启后台自动发布：每轮检查都会向飞书文档写入变更事件。"
            )
        return 0

    def command_service_uninstall(self) -> int:
        manager = self._service_manager()
        result = manager.uninstall()
        if self.json_output:
            self.emit({"ok": True, **result})
            return 0
        if result["removed"]:
            self.console.success(t("后台服务已停止并移除：%s") % result["unit"])
        else:
            self.console.info(t("没有找到已安装的后台服务：%s") % result["unit"])
        return 0

    def command_service_status(self) -> int:
        manager = self._service_manager()
        result = manager.status()
        if self.json_output:
            self.emit({"ok": True, **result})
            return 0
        self.console.header("后台服务", manager.platform)
        self.console.table(
            ["项目", "值"],
            [
                [t("单元文件"), result["unit"]],
                [t("已安装"), t("是") if result["installed"] else t("否")],
                [t("已加载"), t("是") if result["loaded"] else t("否")],
                [t("运行状态"), result.get("pid") or result.get("state") or "-"],
                [t("标准输出日志"), result["stdout_log"]],
                [t("错误日志"), result["stderr_log"]],
            ],
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="branch-radar",
        description=t("本地优先的 Git 分支雷达"),
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument(
        "--lang",
        choices=SUPPORTED_LOCALES,
        help=t("界面语言；默认跟随 BRANCH_RADAR_LANG 或系统区域设置"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help=t("诊断 Git、Codex、飞书与状态目录"))
    _output_flags(doctor)
    doctor.set_defaults(handler="command_doctor")

    discover = subparsers.add_parser("discover", help=t("扫描本地 Git 仓库"))
    discover.add_argument("paths", nargs="*", help=t("扫描根目录"))
    discover.add_argument("--max-depth", type=int, default=None)
    _output_flags(discover)
    discover.set_defaults(handler="command_discover")

    init = subparsers.add_parser("init", help=t("交互初始化"))
    init.add_argument("--root", dest="roots", action="append", help=t("扫描根目录，可重复"))
    init.add_argument("--all", action="store_true", help=t("关注扫描到的所有仓库"))
    init.add_argument("--non-interactive", action="store_true")
    init.add_argument("--no-codex", action="store_true")
    init.add_argument("--no-lark", action="store_true")
    _output_flags(init)
    init.set_defaults(handler="command_init")

    watch = subparsers.add_parser("watch", help=t("管理关注分支"))
    watch_sub = watch.add_subparsers(dest="watch_command", required=True)
    watch_add = watch_sub.add_parser("add", help=t("添加本地仓库"))
    watch_add.add_argument("path")
    watch_add.add_argument("--branch", action="append")
    watch_add.add_argument(
        "--rebaseline",
        action="store_true",
        help=t("明确接受 remote/游标变更，并以当前 SHA 重建静默基线"),
    )
    _output_flags(watch_add)
    watch_add.set_defaults(handler="command_watch_add")
    watch_list = watch_sub.add_parser("list", help=t("查看关注列表"))
    _output_flags(watch_list)
    watch_list.set_defaults(handler="command_watch_list")
    watch_remove = watch_sub.add_parser(
        "remove", help=t("按关注 ID 取消关注（保留历史事件与游标）")
    )
    watch_remove.add_argument(
        "watch_id", type=int, metavar="WATCH_ID",
        help=t("watch list 显示的正整数 ID"),
    )
    _output_flags(watch_remove)
    watch_remove.set_defaults(handler="command_watch_remove")

    check = subparsers.add_parser("check", help=t("检查所有关注分支"))
    check.add_argument("--fetch", action=argparse.BooleanOptionalAction, default=True)
    check.add_argument("--codex", action="store_true", help=t("使用 Codex 生成语义摘要"))
    check.add_argument(
        "--publish",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=t("将新事件同步到飞书文档"),
    )
    _output_flags(check)
    check.set_defaults(handler="command_check")

    status = subparsers.add_parser("status", help=t("查看整体状态"))
    _output_flags(status)
    status.set_defaults(handler="command_status")

    events = subparsers.add_parser("events", help=t("查看最近事件"))
    events.add_argument("--limit", type=int, default=50)
    _output_flags(events)
    events.set_defaults(handler="command_events")

    prune = subparsers.add_parser("prune", help=t("清理过期历史并保留未完成投递"))
    prune.add_argument(
        "--before",
        required=True,
        help=t("清理此时间之前的数据，例如 90d、12w 或 ISO 日期"),
    )
    prune.add_argument("--dry-run", action="store_true", help=t("只预览，不写入数据库"))
    prune.add_argument("--vacuum", action="store_true", help=t("清理后回收数据库空闲页"))
    _output_flags(prune)
    prune.set_defaults(handler="command_prune")

    daemon = subparsers.add_parser("daemon", help=t("前台常驻轮询"))
    daemon.add_argument("--interval", type=int, default=None)
    daemon.add_argument("--codex", action="store_true")
    daemon.add_argument("--publish", action="store_true")
    daemon.add_argument("--once", action="store_true")
    _output_flags(daemon)
    daemon.set_defaults(handler="command_daemon")

    web = subparsers.add_parser("web", help=t("启动本地可视化控制台"))
    web.add_argument("--port", type=int, default=8765, help=t("本地监听端口；0 表示自动选择"))
    web.add_argument(
        "--open",
        dest="open_browser",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=t("启动后自动打开浏览器"),
    )
    web.add_argument("--no-color", action="store_true", help=t("禁用 ANSI 颜色"))
    web.set_defaults(handler="command_web")

    service = subparsers.add_parser("service", help=t("安装或管理后台任务"))
    service_sub = service.add_subparsers(dest="service_command", required=True)

    service_install = service_sub.add_parser("install", help=t("安装并启用后台任务"))
    service_install.add_argument(
        "--mode", choices=["daemon", "web"], default="daemon",
        help=t("daemon 由系统定时唤醒；web 常驻本地控制台"),
    )
    service_install.add_argument("--interval", type=int, default=None, help=t("daemon 检查间隔（秒）"))
    service_install.add_argument("--codex", action="store_true", help=t("后台检查启用 Codex 摘要"))
    service_install.add_argument("--publish", action="store_true", help=t("后台检查自动发布到飞书"))
    service_install.add_argument("--port", type=int, default=8765, help=t("web 模式监听端口"))
    service_install.add_argument("--label", default=None, help=t("自定义服务标签"))
    service_install.add_argument("--dry-run", action="store_true", help=t("只打印将要写入的单元文件"))
    _output_flags(service_install)
    service_install.set_defaults(handler="command_service_install")

    service_uninstall = service_sub.add_parser("uninstall", help=t("停止并移除后台服务"))
    service_uninstall.add_argument("--mode", choices=["daemon", "web"], default="daemon")
    service_uninstall.add_argument("--label", default=None)
    _output_flags(service_uninstall)
    service_uninstall.set_defaults(handler="command_service_uninstall")

    service_status = service_sub.add_parser("status", help=t("查看后台服务状态"))
    service_status.add_argument("--mode", choices=["daemon", "web"], default="daemon")
    service_status.add_argument("--label", default=None)
    _output_flags(service_status)
    service_status.set_defaults(handler="command_service_status")

    lark = subparsers.add_parser("lark", help=t("飞书文档集成"))
    lark_sub = lark.add_subparsers(dest="lark_command", required=True)
    lark_status = lark_sub.add_parser("status", help=t("检查 lark-cli"))
    _output_flags(lark_status)
    lark_status.set_defaults(handler="command_lark_status")
    lark_set = lark_sub.add_parser("set-document", help=t("关联已有文档"))
    lark_set.add_argument("document")
    lark_set.add_argument(
        "--skip-verify",
        action="store_true",
        help=t("离线保存目标，跳过当前可访问性检查"),
    )
    _output_flags(lark_set)
    lark_set.set_defaults(handler="command_lark_set_document")
    lark_create = lark_sub.add_parser("create-document", help=t("创建项目动态文档"))
    lark_create.add_argument("--title", default=None)
    _output_flags(lark_create)
    lark_create.set_defaults(handler="command_lark_create_document")
    lark_publish = lark_sub.add_parser("publish", help=t("发布或重试待投递事件"))
    lark_publish.add_argument("--limit", type=int, default=1000)
    _output_flags(lark_publish)
    lark_publish.set_defaults(handler="command_lark_publish")

    codex = subparsers.add_parser("codex", help=t("Codex CLI 集成"))
    codex_sub = codex.add_subparsers(dest="codex_command", required=True)
    codex_status = codex_sub.add_parser("status", help=t("检查 Codex CLI"))
    _output_flags(codex_status)
    codex_status.set_defaults(handler="command_codex_status")
    return parser


def _output_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help=t("输出机器可读 JSON"))
    parser.add_argument("--no-color", action="store_true", help=t("禁用 ANSI 颜色"))


def _preparse_locale(argv: Sequence[str]) -> str | None:
    """Read --lang before argparse exists.

    argparse resolves help text while the parser is built, which is before it
    could tell us the flag's value, so the flag has to be found first for
    `--lang en --help` to print English.
    """
    values = list(argv)
    for index, value in enumerate(values):
        if value == "--lang" and index + 1 < len(values):
            return values[index + 1]
        if value.startswith("--lang="):
            return value.split("=", 1)[1]
    return None


def main(argv: Sequence[str] | None = None) -> None:
    values = list(sys.argv[1:] if argv is None else argv)
    set_locale(_preparse_locale(values))
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        app = Application(args)
        handler = getattr(app, args.handler)
        code = handler()
    except KeyboardInterrupt:
        code = 130
    except (
        GitError,
        LarkError,
        ServiceInstallError,
        RuntimeError,
        ValueError,
        TypeError,
        OSError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            Console(color=False if getattr(args, "no_color", False) else None).error(
                str(exc)
            )
        code = 1
    raise SystemExit(code)
