from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^/@:]+)@)?(?P<host>[^/:]+):(?P<path>.+)$")
_GITHUB_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
_GITHUB_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+$")
_GITHUB_SCP = re.compile(
    r"^git@github\.com:(?P<owner>[A-Za-z0-9][A-Za-z0-9-]*)/"
    r"(?P<repository>[A-Za-z0-9._-]+?)(?:\.git)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class NormalizedRemote:
    sanitized_url: str
    canonical: str
    host: str
    project_path: str


def _strip_project_suffix(path: str) -> str:
    clean = path.strip().lstrip("/").rstrip("/")
    return clean[:-4] if clean.endswith(".git") else clean


def normalize_remote(url: str, *, base: Path | None = None) -> NormalizedRemote:
    value = url.strip()
    if not value:
        raise ValueError("empty remote URL")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", value):
        raise ValueError("不支持 Git transport helper 形式的远程地址")

    parsed = urlsplit(value)
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError("不支持带远程主机的 file URL")
        resolved = Path(unquote(parsed.path)).resolve()
        project = _strip_project_suffix(resolved.name)
        return NormalizedRemote(str(resolved), "local/%s" % resolved, "local", project)

    scp = _SCP_REMOTE.match(value)
    if scp and "://" not in value:
        host = scp.group("host").lower()
        project = _strip_project_suffix(scp.group("path"))
        user = scp.group("user")
        sanitized = "%s%s:%s%s" % (
            (user + "@") if user else "",
            host,
            project,
            ".git" if value.rstrip("/").endswith(".git") else "",
        )
        return NormalizedRemote(sanitized, "%s/%s" % (host, project), host, project)

    if parsed.scheme and parsed.hostname:
        host = parsed.hostname.lower()
        port = (":" + str(parsed.port)) if parsed.port else ""
        project = _strip_project_suffix(parsed.path)
        username = ""
        if parsed.scheme == "ssh" and parsed.username:
            username = parsed.username + "@"
        sanitized_netloc = "%s%s%s" % (username, host, port)
        suffix = ".git" if parsed.path.rstrip("/").endswith(".git") else ""
        sanitized_path = "/" + project + suffix
        sanitized = urlunsplit((parsed.scheme, sanitized_netloc, sanitized_path, "", ""))
        canonical_host = host + port
        return NormalizedRemote(
            sanitized, "%s/%s" % (canonical_host, project), canonical_host, project
        )

    local = Path(value).expanduser()
    if not local.is_absolute() and base:
        local = base / local
    resolved = local.resolve()
    project = _strip_project_suffix(resolved.name)
    return NormalizedRemote(str(resolved), "local/%s" % resolved, "local", project)


def parse_repository_locator(value: str) -> NormalizedRemote:
    """Turn a pasted repository URL into a comparable remote identity.

    GitHub web pages often include extra path segments such as ``/tree/main``.
    Those are stripped so the locator can match a local checkout's canonical
    remote. Local filesystem paths are rejected because this entry point is for
    associating an already-cloned Git hosting remote.
    """
    text = value.strip()
    if not text:
        raise ValueError("请输入仓库地址")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", text):
        raise ValueError("不支持 Git transport helper 形式的远程地址")
    normalized = normalize_remote(text)
    hostname = normalized.host.split(":", 1)[0].lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if hostname == "local":
        raise ValueError("请输入 GitHub 仓库地址，而不是本地路径")
    parts = [
        item[:-4] if item.lower().endswith(".git") else item
        for item in normalized.project_path.split("/")
        if item
    ]
    if hostname == "github.com":
        if len(parts) < 2:
            raise ValueError("GitHub 地址需要包含 owner/repo")
        project = "%s/%s" % (parts[0], parts[1])
        return NormalizedRemote(
            "https://github.com/%s.git" % project,
            "github.com/%s" % project,
            "github.com",
            project,
        )
    if not parts:
        raise ValueError("无法识别仓库地址")
    return normalized


def parse_github_clone_locator(value: str) -> NormalizedRemote:
    """Validate and sanitize a GitHub URL before passing it to ``git clone``."""
    text = value.strip()
    if not text or any(character in text for character in ("\0", "\r", "\n")):
        raise ValueError("请输入 GitHub 仓库地址")
    if len(text) > 2048:
        raise ValueError("仓库地址过长")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", text):
        raise ValueError("不支持 Git transport helper 形式的远程地址")

    scp = _GITHUB_SCP.fullmatch(text)
    if scp:
        project = _github_project(scp.group("owner"), scp.group("repository"))
        return NormalizedRemote(
            "git@github.com:%s.git" % project,
            "github.com/%s" % project,
            "github.com",
            project,
        )

    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise ValueError("无法识别 GitHub 仓库地址") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "ssh"}:
        raise ValueError("仅支持 GitHub HTTPS 或 SSH 地址")
    if "%" in parsed.netloc or "%" in parsed.path:
        raise ValueError("GitHub 地址不能包含编码后的路径或凭据")
    if "//" in parsed.path.rstrip("/"):
        raise ValueError("GitHub 仓库路径无效")
    if scheme == "https":
        if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
            raise ValueError("仅支持不含凭据和端口的 github.com 地址")
    elif parsed.netloc.lower() != "git@github.com":
        raise ValueError("GitHub SSH 地址必须使用 git@github.com")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub 地址需要包含 owner/repo")
    if scheme == "ssh" and len(parts) != 2:
        raise ValueError("GitHub SSH 地址只能指向仓库根目录")
    if scheme == "https" and len(parts) > 2:
        if len(parts) < 4 or parts[2] != "tree":
            raise ValueError("GitHub 地址只能指向仓库或仓库分支页面")
    repository = parts[1][:-4] if parts[1].lower().endswith(".git") else parts[1]
    project = _github_project(parts[0], repository)
    clone_url = (
        "https://github.com/%s.git" % project
        if scheme == "https"
        else "ssh://git@github.com/%s.git" % project
    )
    return NormalizedRemote(
        clone_url,
        "github.com/%s" % project,
        "github.com",
        project,
    )


def _github_project(owner: str, repository: str) -> str:
    if (
        not _GITHUB_OWNER.fullmatch(owner)
        or not _GITHUB_REPOSITORY.fullmatch(repository)
        or repository in {".", ".."}
        or owner.endswith("-")
        or repository.endswith(".")
    ):
        raise ValueError("GitHub 地址中的 owner 或仓库名无效")
    return "%s/%s" % (owner, repository)
