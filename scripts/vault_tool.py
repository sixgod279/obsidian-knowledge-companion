#!/usr/bin/env python3
"""Deterministic helpers for a concurrent three-layer Obsidian Vault."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time as time_module
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


LOCAL_ZONE = timezone.utc
ARCHIVE_TIME = time(18, 0)
AUTO_ARCHIVE = False
DEFAULT_CONFIG = json.loads(
    (Path(__file__).resolve().parent.parent / "assets" / "config.example.json").read_text(encoding="utf-8")
)
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "obsidian-knowledge-companion" / "config.json"



def configure(args: argparse.Namespace) -> dict:
    global LOCAL_ZONE, ARCHIVE_TIME, AUTO_ARCHIVE
    config = dict(DEFAULT_CONFIG)
    config_path = args.config or os.environ.get("OBSIDIAN_COMPANION_CONFIG")
    if not config_path and DEFAULT_CONFIG_PATH.is_file():
        config_path = str(DEFAULT_CONFIG_PATH)
    if config_path:
        path = Path(config_path).expanduser().resolve()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            for obsolete in ("git_sync_enabled", "sync_weekday", "sync_time", "allow_sensitive_sync"):
                data.pop(obsolete, None)
        if not isinstance(data, dict) or set(data) - set(config):
            raise ValueError("配置必须是 JSON 对象，且不能包含未知字段")
        config.update(data)
        if not isinstance(config["vault"], str) or not config["vault"].strip():
            raise ValueError("vault 必须为非空路径字符串")
        if not Path(config["vault"]).expanduser().is_absolute():
            config["vault"] = str(path.parent / config["vault"])
    for key, default in DEFAULT_CONFIG.items():
        if isinstance(default, bool) and type(config[key]) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    for key in ("archive_time",):
        if not isinstance(config[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config[key]):
            raise ValueError(f"{key} 必须为 HH:MM")
    if not isinstance(config["timezone"], str):
        raise ValueError("timezone 必须为 IANA 时区名称")
    try:
        LOCAL_ZONE = timezone.utc if config["timezone"] == "UTC" else ZoneInfo(config["timezone"])
    except ZoneInfoNotFoundError as error:
        raise ValueError("无法加载时区；请检查名称，Windows 可运行 python -m pip install tzdata") from error
    ARCHIVE_TIME = time.fromisoformat(config["archive_time"])
    AUTO_ARCHIVE = config["auto_archive"]
    config["vault"] = args.vault or os.environ.get("OBSIDIAN_VAULT") or config["vault"]
    if not isinstance(config["vault"], str) or not config["vault"].strip():
        raise ValueError("请通过 --vault、OBSIDIAN_VAULT 或配置文件指定 vault 路径")
    args.vault = str(Path(config["vault"]).expanduser().resolve())
    config["vault"] = args.vault
    return config
INDEX_DIR = "00-检索层"
SOURCE_DIR = "10-资料层"
INBOX_DIR = "90-待入层"
SYSTEM_DIR = Path(INDEX_DIR) / "_系统"
STATE_FILE = SYSTEM_DIR / "archive-state.json"
RUNTIME_VERSION = 2
LEASE_MINUTES = 30
RETENTION_DAYS = 7
SKIP_PARTS = {".obsidian", ".trash", ".git", "__pycache__"}
TEXT_SUFFIXES = {
    ".md", ".txt", ".c", ".h", ".cpp", ".hpp", ".py", ".json",
    ".yaml", ".yml", ".csv", ".log", ".ini", ".cfg", ".xml",
}
STATUS_PATTERNS = {
    "fact": re.compile(r"\[!kb-fact\]", re.IGNORECASE),
    "unverified": re.compile(r"\[!kb-unverified\]", re.IGNORECASE),
    "error": re.compile(r"\[!kb-error\]", re.IGNORECASE),
    "outdated": re.compile(r"\[!kb-outdated\]", re.IGNORECASE),
}
INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WIKI_LINK = re.compile(r"!?\[\[([^\]]+)\]\]")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
LATIN_TOKEN = re.compile(r"[a-z0-9_+.#-]+", re.IGNORECASE)
CHINESE_RUN = re.compile(r"[\u3400-\u9fff]+")
INDEX_SOURCE = re.compile(r"^-\s*资料:\s*(.+)$", re.MULTILINE)
INDEX_HASH = re.compile(r"^-\s*来源哈希:\s*`?(?:sha256:)?([0-9a-fA-F]{64})`?\s*$", re.MULTILINE)
TRANSACTION_STAGES = ("PREPARED", "SNAPSHOT_CREATED", "MUTATING", "VALIDATED", "COMMITTED", "ROLLED_BACK")


def now_local() -> datetime:
    return datetime.now(LOCAL_ZONE)


def stamp(moment: datetime | None = None) -> str:
    return (moment or now_local()).strftime("%Y-%m-%d %H:%M %z")


def iso(moment: datetime | None = None) -> str:
    return (moment or now_local()).isoformat(timespec="seconds")


def emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def read_text(path: Path, limit: int | None = None) -> str:
    data = path.read_bytes()
    if limit is not None and len(data) > limit:
        data = data[:limit]
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_new_text(path: Path, content: str) -> bool:
    """Atomically publish a new file without overwriting a concurrent creator."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.new")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def resolve_vault(value: str) -> Path:
    vault = Path(value).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)
    if not vault.is_dir():
        raise ValueError(f"Vault 不是目录: {vault}")
    return vault


def runtime_root(vault: Path) -> Path:
    return vault.parent / f".{vault.name}-runtime"


def runtime_paths(vault: Path) -> dict[str, Path]:
    root = runtime_root(vault)
    return {
        "root": root,
        "locks": root / "locks.json",
        "guard": root / ".state.guard",
        "transactions": root / "transactions",
        "work_quarantine": root / "work-quarantine",
        "archive_state": root / "archive-state.json",
        "scan": root / "latest-scan.json",
    }


def ensure_runtime(vault: Path) -> dict[str, Path]:
    paths = runtime_paths(vault)
    paths["transactions"].mkdir(parents=True, exist_ok=True)
    paths["work_quarantine"].mkdir(parents=True, exist_ok=True)
    if not paths["locks"].exists():
        save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": []})
    if not paths["archive_state"].exists():
        legacy = vault / STATE_FILE
        legacy_state = load_json(legacy, {}) if legacy.exists() else {}
        if isinstance(legacy_state, dict) and "auto_archive_dates" in legacy_state:
            initial = legacy_state
        else:
            initial = {"schema_version": RUNTIME_VERSION, "auto_archive_dates": [], "history": []}
        save_json(paths["archive_state"], initial)
    return paths


@contextmanager
def state_guard(vault: Path, timeout_seconds: float = 8.0):
    paths = ensure_runtime(vault)
    deadline = time_module.monotonic() + timeout_seconds
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(str(paths["guard"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, f"{os.getpid()} {iso()}".encode("ascii"))
        except FileExistsError:
            try:
                age = time_module.time() - paths["guard"].stat().st_mtime
                if age > 60:
                    paths["guard"].unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time_module.monotonic() >= deadline:
                raise RuntimeError("等待知识库锁状态超时")
            time_module.sleep(0.05)
    try:
        yield paths
    finally:
        os.close(descriptor)
        paths["guard"].unlink(missing_ok=True)


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_hash(path: Path) -> str | None:
    return sha256(path) if path.exists() and path.is_file() else None


def resolve_inside_vault(vault: Path, value: str) -> Path:
    supplied = Path(value)
    candidate = supplied.resolve() if supplied.is_absolute() else (vault / supplied).resolve()
    try:
        candidate.relative_to(vault)
    except ValueError as error:
        raise ValueError(f"路径越过 Vault: {value}") from error
    return candidate


def relative_path(vault: Path, value: str | Path) -> str:
    return resolve_inside_vault(vault, str(value)).relative_to(vault).as_posix()


def normalize_lock_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    return normalized or "."


def paths_conflict(left: str, right: str) -> bool:
    left = normalize_lock_path(left).lower()
    right = normalize_lock_path(right).lower()
    if left == "." or right == ".":
        return True
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def lock_is_expired(lock: dict[str, object], moment: datetime | None = None) -> bool:
    try:
        return parse_datetime(str(lock["expires_at"])) <= (moment or now_local())
    except (KeyError, ValueError):
        return True


def transaction_manifest_path(vault: Path, transaction_id: str) -> Path:
    return ensure_runtime(vault)["transactions"] / transaction_id / "manifest.json"


def transaction_is_open(vault: Path, transaction_id: str | None) -> bool:
    if not transaction_id:
        return False
    manifest = load_json(transaction_manifest_path(vault, transaction_id), {})
    return isinstance(manifest, dict) and manifest.get("stage") not in {"COMMITTED", "ROLLED_BACK", None}


def prune_expired_locks(vault: Path, locks: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    active = []
    expired = []
    for lock in locks:
        if lock_is_expired(lock) and not transaction_is_open(vault, str(lock.get("transaction_id") or "")):
            expired.append(lock)
        else:
            active.append(lock)
    return active, expired


def acquire_lock(vault: Path, owner: str, mode: str, paths_to_lock: list[str], transaction_id: str | None = None) -> dict[str, object]:
    if not owner.strip():
        raise ValueError("owner 不能为空；使用当前会话的 UUID")
    normalized = sorted({normalize_lock_path(relative_path(vault, path)) for path in paths_to_lock})
    with state_guard(vault) as paths:
        state = load_json(paths["locks"], {"locks": []})
        locks = state.get("locks", []) if isinstance(state, dict) else []
        locks, _ = prune_expired_locks(vault, locks)
        same = next((lock for lock in locks if lock.get("owner") == owner and lock.get("mode") == mode and lock.get("transaction_id") == transaction_id), None)
        conflicts = []
        for existing in locks:
            if same and existing.get("lock_id") == same.get("lock_id"):
                continue
            if any(paths_conflict(left, right) for left in normalized for right in existing.get("paths", [])):
                conflicts.append(existing)
        if conflicts:
            raise RuntimeError("锁冲突: " + json.dumps(conflicts, ensure_ascii=False))
        moment = now_local()
        if same:
            same["paths"] = sorted(set(same.get("paths", [])) | set(normalized))
            same["heartbeat_at"] = iso(moment)
            same["expires_at"] = iso(moment + timedelta(minutes=LEASE_MINUTES))
            record = same
        else:
            record = {
                "lock_id": uuid.uuid4().hex,
                "owner": owner,
                "mode": mode,
                "paths": normalized,
                "transaction_id": transaction_id,
                "acquired_at": iso(moment),
                "heartbeat_at": iso(moment),
                "expires_at": iso(moment + timedelta(minutes=LEASE_MINUTES)),
            }
            locks.append(record)
        save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": locks})
    return record


def initial_index(title: str, purpose: str) -> str:
    return (
        "---\n"
        "type: index\n"
        f"title: {title}\ncreated: {stamp()}\nupdated: {stamp()}\n"
        "---\n\n"
        f"# {title}\n\n{purpose}\n\n"
        "> [!note] 使用规则\n"
        "> 本文件只保存短路由。详细结论位于资料层，并通过有效 Obsidian 链接指向它。\n"
    )


def command_init(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    directories = [Path(INDEX_DIR) / "_系统"] + [
        Path(SOURCE_DIR) / "01-原始资料" / name
        for name in ("芯片与器件手册", "电路与硬件", "规格与需求", "日志与测试数据", "其他资料")
    ] + [
        Path(SOURCE_DIR) / "02-经验知识" / name
        for name in ("项目", "故障与解决方案", "编码要点", "调试方法", "决策记录", "个人偏好")
    ] + [Path(INBOX_DIR) / "_模板"]
    for relative in directories:
        (vault / relative).mkdir(parents=True, exist_ok=True)
    index_files = {
        "知识库入口.md": "知识库总入口。先从专题目录选择一个或多个路由。",
        "项目目录.md": "按项目、产品和仓库定位资料。",
        "故障目录.md": "按现象、根因和解决方案定位历史故障。",
        "技术主题目录.md": "按器件、协议、模块、语言和工程主题定位资料。",
        "决策与偏好目录.md": "按项目决策、工作方式和个人偏好定位资料。",
        "原始资料目录.md": "按器件型号、资料类型、版本和来源定位原始资料。",
    }
    created = []
    for filename, purpose in index_files.items():
        path = vault / INDEX_DIR / filename
        if not path.exists():
            write_text(path, initial_index(path.stem, purpose))
            created.append(path.relative_to(vault).as_posix())
    for template in (skill_root() / "assets").glob("*.md"):
        target = vault / INBOX_DIR / "_模板" / template.name
        if not target.exists():
            shutil.copy2(template, target)
            created.append(target.relative_to(vault).as_posix())
    paths = ensure_runtime(vault)
    emit({"ok": True, "vault": str(vault), "runtime": str(paths["root"]), "created": created})
    return 0


def inbox_for(vault: Path, day: date) -> Path:
    return vault / INBOX_DIR / f"{day:%Y}" / f"{day:%Y-%m-%d}"


def dated_inbox_files(vault: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    root = vault / INBOX_DIR
    if not root.exists():
        return {}
    for path in root.rglob("*"):
        if not path.is_file() or "_模板" in path.parts:
            continue
        relative_parts = path.relative_to(root).parts
        if len(relative_parts) < 3 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", relative_parts[1]):
            continue
        result[relative_parts[1]].append(path.relative_to(vault).as_posix())
    return {day: sorted(files) for day, files in sorted(result.items())}


def command_check_archive(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    moment = now_local()
    today = moment.date().isoformat()
    state = load_json(ensure_runtime(vault)["archive_state"], {})
    auto_dates = set(state.get("auto_archive_dates", [])) if isinstance(state, dict) else set()
    dated = dated_inbox_files(vault)
    overdue = [day for day in dated if day < today]
    after_cutoff = moment.time() >= ARCHIVE_TIME
    today_due = bool(dated.get(today) and after_cutoff and today not in auto_dates)
    if args.manual:
        pending_dates = list(dated)
        reason = "manual" if pending_dates else "no-inbox-content"
    elif not AUTO_ARCHIVE:
        pending_dates = []
        reason = "auto-archive-disabled"
    else:
        pending_dates = (overdue + ([today] if today_due else [])) if after_cutoff else []
        if overdue and after_cutoff:
            reason = "overdue-inbox"
        elif today_due:
            reason = "after-cutoff-first-archive"
        elif overdue:
            reason = "before-cutoff-with-overdue-inbox"
        elif dated.get(today) and today in auto_dates:
            reason = "today-already-auto-archived-new-content-waits"
        elif dated.get(today):
            reason = "before-cutoff"
        else:
            reason = "no-inbox-content"
    manifest = [path for day in pending_dates for path in dated.get(day, [])]
    emit({
        "due": bool(pending_dates),
        "reason": reason,
        "local_now": stamp(moment),
        "pending_dates": pending_dates,
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256("\n".join(manifest).encode("utf-8")).hexdigest(),
        "auto_archive_dates": sorted(auto_dates),
    })
    return 0


def tokens(text: str) -> list[str]:
    lowered = text.lower()
    result = LATIN_TOKEN.findall(lowered)
    for run in CHINESE_RUN.findall(lowered):
        result.append(run)
        if len(run) == 1:
            continue
        result.extend(run[index:index + 2] for index in range(len(run) - 1))
        if len(run) > 3:
            result.extend(run[index:index + 3] for index in range(len(run) - 2))
    return result


def query_terms(query: str) -> set[str]:
    return set(tokens(query))


def snippets(text: str, terms: Iterable[str], limit: int = 3) -> list[str]:
    ranked = []
    for position, line in enumerate(line.strip() for line in text.splitlines() if line.strip()):
        lowered = line.lower()
        hits = sum(1 for term in terms if term in lowered)
        if hits:
            ranked.append((-hits, position, line[:240]))
    return [line for _, _, line in sorted(ranked)[:limit]]


def command_search_index(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    root = vault / INDEX_DIR
    query = query_terms(args.query)
    results = []
    files = sorted(root.glob("*.md")) if root.exists() else []
    for path in files:
        text = read_text(path, 2_000_000)
        document_terms = Counter(tokens(path.stem + "\n" + text))
        matched = query & set(document_terms)
        if not matched:
            continue
        score = sum(1 + math.log1p(document_terms[term]) for term in matched)
        score += sum(3 for term in matched if term in path.stem.lower())
        results.append({"score": round(score, 4), "path": str(path), "matched_terms": sorted(matched)[:20], "snippets": snippets(text, matched)})
    results.sort(key=lambda item: (-item["score"], item["path"]))
    emit({"query": args.query, "searched_root": str(root), "searched_files": len(files), "results": results[:args.limit], "can_expand_to_source_layer": bool(results)})
    return 0


def safe_filename(title: str) -> str:
    value = INVALID_FILENAME.sub("-", title).strip().rstrip(".")
    return re.sub(r"\s+", " ", value)[:100] or "未命名问题"


def note_template(title: str, kind: str, project: str, moment: datetime) -> str:
    return (
        "---\n"
        f"type: {kind}\ntitle: {title}\ncreated: {stamp(moment)}\nupdated: {stamp(moment)}\n"
        f"project: {project or '未指定'}\ntopics: []\nscope: {{}}\nsources: []\n---\n\n"
        f"# {title}\n\n## 现象与背景\n\n"
        f"> [!kb-unverified] 未验证 | 记录: {stamp(moment)} | 复核期限: 未定\n"
        "> 在此填写当前已知现象、条件或用户陈述；核验后再变更状态。\n"
        "> 待验证: 填写能够确认或证伪该信息的检查。\n\n"
        "## 证据与排查\n\n## 根因与结论\n\n## 方案与验证\n\n## 遗留风险\n"
    )


def command_new_note(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    moment = now_local()
    target_day = date.fromisoformat(args.date) if args.date else moment.date()
    folder = inbox_for(vault, target_day)
    path = folder / (safe_filename(args.title) + ".md")
    attachment = folder / "附件" / path.stem
    lock = acquire_lock(vault, args.owner, "work", [str(path), str(attachment)])
    created = False
    if not path.exists():
        created = write_new_text(path, note_template(args.title, args.kind, args.project, moment))
    emit({"ok": True, "created": created, "path": str(path), "attachment_directory": str(attachment), "expected_sha256": sha256(path), "lock": lock})
    return 0


def command_propose_index_update(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    index = resolve_inside_vault(vault, args.index)
    source = resolve_inside_vault(vault, args.source)
    if not index.is_file() or not source.is_file():
        raise ValueError("索引和来源必须是现有文件")
    if index.relative_to(vault).parts[0] != INDEX_DIR:
        raise ValueError("--index 必须位于检索层")
    title = f"索引修订-{index.stem}-{source.stem}"
    moment = now_local()
    folder = inbox_for(vault, moment.date())
    path = folder / (safe_filename(title) + ".md")
    lock = acquire_lock(vault, args.owner, "work", [str(path)])
    if not path.exists():
        content = (
            "---\ntype: index-proposal\n"
            f"title: {title}\ncreated: {stamp(moment)}\nupdated: {stamp(moment)}\n"
            f"project: 知识库维护\ntopics: [索引修订]\nscope: {{}}\n"
            f"sources:\n  - {source.relative_to(vault).as_posix()}\n---\n\n"
            f"# {title}\n\n"
            f"> [!kb-fact] 事实 | 记录: {stamp(moment)} | 核验: {stamp(moment)} | 方式: 文件哈希\n"
            f"> 目标索引: `[[{index.relative_to(vault).with_suffix('').as_posix()}]]`，旧索引 SHA-256: `{sha256(index)}`。\n"
            f"> 来源资料: `[[{source.relative_to(vault).with_suffix('').as_posix()}]]`，来源 SHA-256: `{sha256(source)}`。\n\n"
            f"> [!kb-unverified] 未验证 | 记录: {stamp(moment)} | 复核期限: 下次归档\n"
            f"> 修订原因: {args.reason}\n"
            "> 建议目录项: 在此写入经全文阅读后形成的准确、充分目录项；归档时重新核对双方哈希。\n"
        )
        write_new_text(path, content)
    emit({"ok": True, "path": str(path), "expected_sha256": sha256(path), "lock": lock})
    return 0


def command_heartbeat(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    with state_guard(vault) as paths:
        state = load_json(paths["locks"], {"locks": []})
        locks = state.get("locks", []) if isinstance(state, dict) else []
        moment = now_local()
        updated = []
        for lock in locks:
            if lock.get("owner") == args.owner:
                lock["heartbeat_at"] = iso(moment)
                lock["expires_at"] = iso(moment + timedelta(minutes=LEASE_MINUTES))
                updated.append(lock.get("lock_id"))
        save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": locks})
    emit({"ok": bool(updated), "owner": args.owner, "updated_lock_ids": updated})
    return 0 if updated else 2


def command_release_lock(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    with state_guard(vault) as paths:
        state = load_json(paths["locks"], {"locks": []})
        locks = state.get("locks", []) if isinstance(state, dict) else []
        kept = []
        released = []
        retained_transaction_locks = []
        for lock in locks:
            matches = lock.get("owner") == args.owner and (not args.lock_id or lock.get("lock_id") == args.lock_id)
            if matches and transaction_is_open(vault, str(lock.get("transaction_id") or "")):
                kept.append(lock)
                retained_transaction_locks.append(lock.get("lock_id"))
            elif matches:
                released.append(lock.get("lock_id"))
            else:
                kept.append(lock)
        save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": kept})
    emit({"ok": True, "released_lock_ids": released, "open_transaction_locks_retained": retained_transaction_locks})
    return 0


def owner_holds_path(vault: Path, owner: str, relative: str, allowed_modes: set[str]) -> bool:
    with state_guard(vault) as paths:
        state = load_json(paths["locks"], {"locks": []})
        locks = state.get("locks", []) if isinstance(state, dict) else []
        moment = now_local()
        matched = False
        for lock in locks:
            covers = any(relative.lower() == held.lower() or relative.lower().startswith(held.lower().rstrip("/") + "/") for held in lock.get("paths", []))
            if lock.get("owner") == owner and lock.get("mode") in allowed_modes and not lock_is_expired(lock, moment) and covers:
                lock["heartbeat_at"] = iso(moment)
                lock["expires_at"] = iso(moment + timedelta(minutes=LEASE_MINUTES))
                matched = True
        if matched:
            save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": locks})
        return matched


def command_safe_write(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    target = resolve_inside_vault(vault, args.path)
    relative = target.relative_to(vault).as_posix()
    if not relative.startswith(INBOX_DIR + "/") or "/_模板/" in "/" + relative:
        raise ValueError("普通 safe-write 只能写待入层的非模板路径")
    if not owner_holds_path(vault, args.owner, relative, {"work"}):
        raise RuntimeError(f"owner 未持有目标路径锁: {relative}")
    actual = file_hash(target)
    expected = None if args.expected_sha256 == "NONE" else args.expected_sha256.lower()
    if actual != expected:
        emit({"ok": False, "conflict": {"path": relative, "expected_sha256": expected, "actual_sha256": actual}})
        return 3
    source = Path(args.input).resolve()
    content = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    emit({"ok": True, "path": relative, "sha256": sha256(target)})
    return 0


def command_safe_delete(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    target = resolve_inside_vault(vault, args.path)
    relative = target.relative_to(vault).as_posix()
    if not relative.startswith(INBOX_DIR + "/") or "/_模板/" in "/" + relative:
        raise ValueError("普通 safe-delete 只能删除待入层的非模板文件")
    if not target.is_file():
        raise ValueError("safe-delete 目标必须是现有文件")
    if not owner_holds_path(vault, args.owner, relative, {"work"}):
        raise RuntimeError(f"owner 未持有目标路径锁: {relative}")
    actual = sha256(target)
    if actual != args.expected_sha256.lower():
        emit({"ok": False, "conflict": {"path": relative, "expected_sha256": args.expected_sha256.lower(), "actual_sha256": actual}})
        return 3
    paths = ensure_runtime(vault)
    operation = f"{now_local():%Y%m%d-%H%M%S}-{uuid.uuid4().hex}"
    destination = paths["work_quarantine"] / operation / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, destination)
    save_json(paths["work_quarantine"] / operation / "manifest.json", {"schema_version": RUNTIME_VERSION, "completed_at": iso(), "owner": args.owner, "source": relative, "sha256": actual, "quarantine": str(destination)})
    emit({"ok": True, "path": relative, "quarantine": str(destination), "retention_days": RETENTION_DAYS})
    return 0


def iter_files(vault: Path) -> Iterable[Path]:
    for path in vault.rglob("*"):
        if path.is_file() and not any(part in SKIP_PARTS for part in path.parts):
            yield path


def parse_frontmatter(text: str) -> dict[str, str]:
    match = FRONTMATTER.match(text)
    if not match:
        return {}
    result = {}
    current = None
    for line in match.group(1).splitlines():
        key_match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if key_match:
            current = key_match.group(1).lower()
            result[current] = key_match.group(2).strip()
        elif current and line.strip().startswith("-"):
            result[current] = (result[current] + " " + line.strip()[1:].strip()).strip()
        elif current and line.startswith(" "):
            result[current] = (result[current] + " " + line.strip()).strip()
    return result


def markdown_inventory(vault: Path, roots: tuple[str, ...] = (INDEX_DIR, SOURCE_DIR, INBOX_DIR)) -> list[dict[str, object]]:
    inventory = []
    for root_name in roots:
        root = vault / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            if "_模板" in path.parts or any(part in SKIP_PARTS for part in path.parts):
                continue
            text = read_text(path, 4_000_000)
            relative = path.relative_to(vault).as_posix()
            metadata = parse_frontmatter(text)
            inventory.append({
                "path": relative,
                "title": metadata.get("title", path.stem),
                "project": metadata.get("project", ""),
                "scope": metadata.get("scope", ""),
                "topics": metadata.get("topics", ""),
                "text": text,
                "tokens": tokens(text),
                "links": [value.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/") for value in WIKI_LINK.findall(text)],
                "status_counts": {name: len(pattern.findall(text)) for name, pattern in STATUS_PATTERNS.items()},
            })
    return inventory


def bm25_search(inventory: list[dict[str, object]], query: str, limit: int) -> list[dict[str, object]]:
    query_tokens = tokens(query)
    if not query_tokens or not inventory:
        return []
    document_frequency = Counter()
    lengths = []
    counters = []
    for item in inventory:
        counter = Counter(item["tokens"])
        counters.append(counter)
        lengths.append(sum(counter.values()))
        document_frequency.update(counter.keys())
    average_length = sum(lengths) / max(len(lengths), 1)
    results = []
    for index, item in enumerate(inventory):
        counter = counters[index]
        score = 0.0
        signals = Counter()
        title_terms = set(tokens(str(item["title"])))
        metadata_terms = set(tokens(" ".join(str(item[key]) for key in ("project", "scope", "topics"))))
        path_terms = set(tokens(str(item["path"])))
        link_terms = set(tokens(" ".join(item["links"])))
        for term in query_tokens:
            frequency = counter.get(term, 0)
            if frequency:
                inverse = math.log(1 + (len(inventory) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
                denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * lengths[index] / max(average_length, 1))
                score += inverse * frequency * 2.2 / denominator
                signals["bm25"] += 1
            if term in title_terms:
                score += 3.0
                signals["title"] += 1
            if term in metadata_terms:
                score += 2.0
                signals["metadata"] += 1
            if term in path_terms:
                score += 1.0
                signals["path"] += 1
            if term in link_terms:
                score += 0.75
                signals["links"] += 1
        if score > 0:
            results.append({
                "score": round(score, 4),
                "path": item["path"],
                "title": item["title"],
                "project": item["project"],
                "scope": item["scope"],
                "signals": dict(signals),
                "status_counts": item["status_counts"],
                "snippets": snippets(str(item["text"]), set(query_tokens)),
                "required_semantic_decision": ["完全重复", "语义重复", "版本并存", "真实冲突", "无关"],
            })
    results.sort(key=lambda item: (-item["score"], item["path"]))
    return results[:limit]


def command_candidate_search(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    inventory = markdown_inventory(vault, (SOURCE_DIR, INBOX_DIR))
    emit({"query": args.query, "searched_documents": len(inventory), "results": bm25_search(inventory, args.query, args.limit), "warning": "分数只用于候选召回；合并前必须阅读全文并判断范围、结论和证据。"})
    return 0


def normalized_markdown(text: str) -> str:
    text = FRONTMATTER.sub("", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"\s+", "", text).lower()


def exact_groups(mapping: dict[str, list[str]]) -> list[dict[str, object]]:
    return [{"key": key, "files": sorted(files)} for key, files in mapping.items() if key and len(files) > 1]


def link_inventory(inventory: list[dict[str, object]]) -> tuple[set[str], dict[str, list[str]]]:
    exact = set()
    stems: dict[str, list[str]] = defaultdict(list)
    for item in inventory:
        path = str(item["path"])
        without = path[:-3] if path.lower().endswith(".md") else path
        exact.add(without.lower())
        stems[Path(without).name.lower()].append(path)
    return exact, stems


def broken_links(inventory: list[dict[str, object]]) -> list[dict[str, str]]:
    exact, stems = link_inventory(inventory)
    result = []
    for item in inventory:
        for target in item["links"]:
            if not target or "://" in target:
                continue
            normalized = target[:-3] if target.lower().endswith(".md") else target
            normalized = normalized.lstrip("/").lower()
            if normalized not in exact and Path(normalized).name not in stems:
                result.append({"source": str(item["path"]), "target": target})
    return result


def status_schema_issues(inventory: list[dict[str, object]]) -> list[dict[str, object]]:
    requirements = {
        "kb-fact": ("记录:", "核验:", "方式:"),
        "kb-unverified": ("记录:",),
        "kb-error": ("记录:", "判错:", "学习价值:"),
        "kb-outdated": ("记录:", "判定:", "原适用:"),
    }
    header = re.compile(r"^\s*>\s*\[!(kb-(?:fact|unverified|error|outdated))\](.*)$", re.IGNORECASE)
    issues = []
    for item in inventory:
        for number, line in enumerate(str(item["text"]).splitlines(), start=1):
            match = header.match(line)
            if not match:
                continue
            missing = [field for field in requirements[match.group(1).lower()] if field not in match.group(2)]
            if match.group(1).lower() == "kb-outdated" and "替代:" not in match.group(2) and "现适用:" not in match.group(2):
                missing.append("替代: 或 现适用:")
            if missing:
                issues.append({"path": item["path"], "line": number, "status": match.group(1).lower(), "missing": missing})
    return issues


def index_audit(vault: Path, inventory: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[str]]:
    referenced = set()
    stale = []
    lookup = {str(item["path"]).lower(): item for item in inventory}
    stem_lookup: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in inventory:
        stem_lookup[Path(str(item["path"])).stem.lower()].append(item)
    for item in inventory:
        if not str(item["path"]).startswith(INDEX_DIR + "/"):
            continue
        sections = re.split(r"(?=^##\s+)", str(item["text"]), flags=re.MULTILINE)
        for section in sections:
            links_match = INDEX_SOURCE.search(section)
            if not links_match:
                continue
            linked = [value.split("|", 1)[0].split("#", 1)[0].strip().replace("\\", "/") for value in WIKI_LINK.findall(links_match.group(1))]
            expected_match = INDEX_HASH.search(section)
            expected = expected_match.group(1).lower() if expected_match else None
            for target in linked:
                normalized = target[:-3] if target.lower().endswith(".md") else target
                candidate = lookup.get((normalized + ".md").lower()) or lookup.get(normalized.lower())
                if not candidate:
                    matches = stem_lookup.get(Path(normalized).name.lower(), [])
                    candidate = matches[0] if len(matches) == 1 else None
                if candidate and str(candidate["path"]).startswith(SOURCE_DIR + "/"):
                    referenced.add(str(candidate["path"]))
                    actual = sha256(vault / str(candidate["path"]))
                    if not expected:
                        stale.append({"index": item["path"], "source": candidate["path"], "issue": "missing-source-hash", "actual_sha256": actual})
                    elif expected != actual:
                        stale.append({"index": item["path"], "source": candidate["path"], "issue": "source-hash-mismatch", "expected_sha256": expected, "actual_sha256": actual})
    source_documents = {str(item["path"]) for item in inventory if str(item["path"]).startswith(SOURCE_DIR + "/")}
    return stale, sorted(source_documents - referenced)


def command_scan(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    files = list(iter_files(vault))
    hashes: dict[str, list[str]] = defaultdict(list)
    for path in files:
        hashes[sha256(path)].append(path.relative_to(vault).as_posix())
    inventory = markdown_inventory(vault)
    normalized: dict[str, list[str]] = defaultdict(list)
    titles: dict[str, list[str]] = defaultdict(list)
    totals = Counter()
    for item in inventory:
        body = normalized_markdown(str(item["text"]))
        if body:
            normalized[hashlib.sha256(body.encode("utf-8")).hexdigest()].append(str(item["path"]))
        titles[str(item["title"]).lower()].append(str(item["path"]))
        if str(item["path"]).startswith((SOURCE_DIR + "/", INBOX_DIR + "/")):
            totals.update(item["status_counts"])
    stale_index, orphan_sources = index_audit(vault, inventory)
    report = {
        "schema_version": RUNTIME_VERSION,
        "generated_at": stamp(),
        "summary": {"files": len(files), "markdown_files": len(inventory), "status_blocks": dict(totals)},
        "exact_file_duplicates": exact_groups(hashes),
        "normalized_markdown_duplicates": exact_groups(normalized),
        "duplicate_titles": [{"title": title, "files": sorted(paths)} for title, paths in titles.items() if len(paths) > 1],
        "broken_wiki_links": broken_links(inventory),
        "status_schema_issues": status_schema_issues([item for item in inventory if str(item["path"]).startswith((SOURCE_DIR + "/", INBOX_DIR + "/"))]),
        "stale_index_entries": stale_index,
        "orphan_source_documents": orphan_sources,
        "inbox_files_by_date": dated_inbox_files(vault),
    }
    output = Path(args.output).resolve() if args.output else ensure_runtime(vault)["scan"]
    save_json(output, report)
    emit({"ok": True, "report": str(output), **report["summary"], "issue_counts": {key: len(report[key]) for key in ("exact_file_duplicates", "normalized_markdown_duplicates", "duplicate_titles", "broken_wiki_links", "status_schema_issues", "stale_index_entries", "orphan_source_documents")}})
    return 0


def transaction_root(vault: Path, transaction_id: str) -> Path:
    return ensure_runtime(vault)["transactions"] / transaction_id


def save_manifest(vault: Path, manifest: dict[str, object]) -> None:
    save_json(transaction_manifest_path(vault, str(manifest["transaction_id"])), manifest)


def snapshot_transaction_path(vault: Path, manifest: dict[str, object], relative: str) -> dict[str, object]:
    source = vault / relative
    existing = next((item for item in manifest["files"] if item["path"] == relative), None)
    if existing:
        return existing
    record = {"path": relative, "existed": source.exists(), "baseline_sha256": file_hash(source), "current_sha256": file_hash(source)}
    if source.exists():
        if not source.is_file():
            raise ValueError(f"事务路径必须展开为文件: {relative}")
        target = transaction_root(vault, str(manifest["transaction_id"])) / "snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        before = sha256(source)
        shutil.copy2(source, target)
        after = sha256(source)
        if before != after or sha256(target) != before:
            raise RuntimeError(f"建立快照时文件发生变化: {relative}")
        record["baseline_sha256"] = before
        record["current_sha256"] = before
        record["snapshot"] = target.relative_to(runtime_root(vault)).as_posix()
    manifest["files"].append(record)
    return record


def command_begin_transaction(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest_paths = sorted({relative_path(vault, value) for value in args.path})
    transaction_id = uuid.uuid4().hex
    lock_paths = [INDEX_DIR, SOURCE_DIR] + manifest_paths
    lock = acquire_lock(vault, args.owner, args.mode, lock_paths, transaction_id)
    manifest = {
        "schema_version": RUNTIME_VERSION,
        "transaction_id": transaction_id,
        "mode": args.mode,
        "owner": args.owner,
        "stage": "PREPARED",
        "created_at": iso(),
        "updated_at": iso(),
        "lock_id": lock["lock_id"],
        "files": [],
        "operations": [],
        "conflicts": [],
    }
    transaction_root(vault, transaction_id).mkdir(parents=True, exist_ok=False)
    save_manifest(vault, manifest)
    try:
        for relative in manifest_paths:
            snapshot_transaction_path(vault, manifest, relative)
        manifest["stage"] = "SNAPSHOT_CREATED"
        manifest["updated_at"] = iso()
        save_manifest(vault, manifest)
    except Exception as error:
        manifest["initialization_error"] = str(error)
        manifest["updated_at"] = iso()
        save_manifest(vault, manifest)
        raise
    emit({"ok": True, "transaction_id": transaction_id, "stage": manifest["stage"], "lock": lock, "files": manifest["files"]})
    return 0


def load_open_transaction(vault: Path, transaction_id: str, owner: str | None = None) -> dict[str, object]:
    manifest = load_json(transaction_manifest_path(vault, transaction_id), {})
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError(f"事务不存在: {transaction_id}")
    if owner and manifest.get("owner") != owner:
        raise ValueError("事务 owner 不匹配")
    if manifest.get("stage") in {"COMMITTED", "ROLLED_BACK"}:
        raise ValueError(f"事务已结束: {manifest.get('stage')}")
    return manifest


def command_extend_transaction(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_open_transaction(vault, args.transaction_id, args.owner)
    new_paths = sorted({relative_path(vault, value) for value in args.path})
    acquire_lock(vault, args.owner, str(manifest["mode"]), new_paths, args.transaction_id)
    added = []
    for relative in new_paths:
        before = len(manifest["files"])
        snapshot_transaction_path(vault, manifest, relative)
        if len(manifest["files"]) > before:
            added.append(relative)
    manifest["updated_at"] = iso()
    save_manifest(vault, manifest)
    emit({"ok": True, "transaction_id": args.transaction_id, "added": added})
    return 0


def transaction_file(manifest: dict[str, object], relative: str) -> dict[str, object]:
    record = next((item for item in manifest["files"] if item["path"] == relative), None)
    if not record:
        raise ValueError(f"路径未加入事务: {relative}")
    return record


def command_atomic_write(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_open_transaction(vault, args.transaction_id, args.owner)
    if manifest["stage"] not in {"SNAPSHOT_CREATED", "MUTATING"}:
        raise ValueError(f"当前阶段不允许写入: {manifest['stage']}")
    relative = relative_path(vault, args.path)
    record = transaction_file(manifest, relative)
    target = vault / relative
    actual = file_hash(target)
    expected = record.get("current_sha256")
    if actual != expected:
        conflict_root = transaction_root(vault, args.transaction_id) / "conflicts"
        conflict_root.mkdir(parents=True, exist_ok=True)
        input_path = Path(args.input).resolve()
        conflict = conflict_root / (safe_filename(Path(relative).name) + f".{uuid.uuid4().hex}.proposed")
        shutil.copy2(input_path, conflict)
        detail = {"path": relative, "expected_sha256": expected, "actual_sha256": actual, "proposal": str(conflict)}
        manifest["conflicts"].append(detail)
        save_manifest(vault, manifest)
        emit({"ok": False, "conflict": detail})
        return 3
    content = Path(args.input).read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    record["current_sha256"] = sha256(target)
    manifest["stage"] = "MUTATING"
    manifest["updated_at"] = iso()
    manifest["operations"].append({"operation": "atomic-write", "path": relative, "at": iso(), "sha256": record["current_sha256"]})
    save_manifest(vault, manifest)
    emit({"ok": True, "path": relative, "sha256": record["current_sha256"], "stage": manifest["stage"]})
    return 0


def command_accept_external(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_open_transaction(vault, args.transaction_id, args.owner)
    relative = relative_path(vault, args.path)
    record = transaction_file(manifest, relative)
    if any(operation.get("path") == relative for operation in manifest["operations"]):
        raise ValueError("该路径已有事务操作，不能把外部变化直接接受为新基线")
    target = vault / relative
    actual = file_hash(target)
    if actual is None:
        raise ValueError("外部基线必须是现有文件")
    snapshot = transaction_root(vault, args.transaction_id) / "snapshot" / relative
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    temporary = snapshot.with_name(f".{snapshot.name}.{uuid.uuid4().hex}.refresh")
    shutil.copy2(target, temporary)
    if sha256(temporary) != actual:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("接受外部变化时文件再次改变")
    os.replace(temporary, snapshot)
    record.update({
        "existed": True,
        "baseline_sha256": actual,
        "current_sha256": actual,
        "snapshot": snapshot.relative_to(runtime_root(vault)).as_posix(),
    })
    manifest["conflicts"] = [conflict for conflict in manifest["conflicts"] if conflict.get("path") != relative]
    manifest["updated_at"] = iso()
    save_manifest(vault, manifest)
    emit({"ok": True, "path": relative, "new_baseline_sha256": actual})
    return 0


def command_quarantine(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_open_transaction(vault, args.transaction_id, args.owner)
    if manifest["stage"] not in {"SNAPSHOT_CREATED", "MUTATING"}:
        raise ValueError(f"当前阶段不允许隔离: {manifest['stage']}")
    relative = relative_path(vault, args.path)
    record = transaction_file(manifest, relative)
    source = vault / relative
    actual = file_hash(source)
    if actual != record.get("current_sha256"):
        raise RuntimeError(f"隔离前哈希冲突: {relative}")
    target = transaction_root(vault, args.transaction_id) / "quarantine" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    record["current_sha256"] = None
    manifest["stage"] = "MUTATING"
    manifest["updated_at"] = iso()
    manifest["operations"].append({"operation": "quarantine", "path": relative, "target": str(target), "at": iso()})
    save_manifest(vault, manifest)
    emit({"ok": True, "path": relative, "quarantine": str(target)})
    return 0


def command_advance_transaction(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_open_transaction(vault, args.transaction_id, args.owner)
    allowed = {"SNAPSHOT_CREATED": {"MUTATING", "VALIDATED"}, "MUTATING": {"VALIDATED"}, "VALIDATED": {"COMMITTED"}}
    target = args.stage.upper()
    if target not in allowed.get(str(manifest["stage"]), set()):
        raise ValueError(f"非法阶段迁移: {manifest['stage']} -> {target}")
    mismatches = []
    for record in manifest["files"]:
        actual = file_hash(vault / record["path"])
        if actual != record.get("current_sha256"):
            mismatches.append({"path": record["path"], "expected": record.get("current_sha256"), "actual": actual})
    if mismatches:
        emit({"ok": False, "error": "事务文件出现未记录变化", "mismatches": mismatches})
        return 3
    if target == "COMMITTED" and manifest.get("mode") == "archive":
        remaining_inbox = [
            record["path"] for record in manifest["files"]
            if record.get("existed") and str(record["path"]).startswith(INBOX_DIR + "/") and record.get("current_sha256") is not None
        ]
        if remaining_inbox:
            emit({"ok": False, "error": "归档清单中的原始待入文件尚未归入资料层或隔离", "remaining_inbox": remaining_inbox})
            return 3
    manifest["stage"] = target
    manifest["updated_at"] = iso()
    if target == "COMMITTED":
        manifest["completed_at"] = iso()
    save_manifest(vault, manifest)
    if target == "COMMITTED":
        release_owner_transaction_lock(vault, args.owner, args.transaction_id)
    emit({"ok": True, "transaction_id": args.transaction_id, "stage": target})
    return 0


def release_owner_transaction_lock(vault: Path, owner: str, transaction_id: str) -> None:
    with state_guard(vault) as paths:
        state = load_json(paths["locks"], {"locks": []})
        locks = state.get("locks", []) if isinstance(state, dict) else []
        locks = [lock for lock in locks if not (lock.get("owner") == owner and lock.get("transaction_id") == transaction_id)]
        save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": locks})


def command_rollback_transaction(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_open_transaction(vault, args.transaction_id, args.owner)
    conflicts = []
    for record in manifest["files"]:
        target = vault / record["path"]
        actual = file_hash(target)
        if actual != record.get("current_sha256"):
            conflicts.append({"path": record["path"], "expected": record.get("current_sha256"), "actual": actual})
    if conflicts:
        emit({"ok": False, "error": "回滚会覆盖事务外变化", "conflicts": conflicts})
        return 3
    for record in manifest["files"]:
        target = vault / record["path"]
        if record["existed"]:
            snapshot = runtime_root(vault) / record["snapshot"]
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
            shutil.copy2(snapshot, temporary)
            os.replace(temporary, target)
        elif target.exists() and target.is_file():
            target.unlink()
    manifest["stage"] = "ROLLED_BACK"
    manifest["completed_at"] = iso()
    manifest["updated_at"] = iso()
    save_manifest(vault, manifest)
    release_owner_transaction_lock(vault, args.owner, args.transaction_id)
    emit({"ok": True, "transaction_id": args.transaction_id, "stage": "ROLLED_BACK"})
    return 0


def command_mark_archived(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    manifest = load_json(transaction_manifest_path(vault, args.transaction_id), {})
    if not isinstance(manifest, dict) or manifest.get("stage") != "COMMITTED" or manifest.get("mode") != "archive":
        raise ValueError("只能为已 COMMITTED 的 archive 事务标记归档成功")
    target_dates = sorted(set(args.date))
    state_path = ensure_runtime(vault)["archive_state"]
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    auto_dates = set(state.get("auto_archive_dates", []))
    if args.auto:
        auto_dates.update(target_dates)
    history = state.get("history", []) if isinstance(state.get("history", []), list) else []
    history.append({"dates": target_dates, "completed_at": stamp(), "summary": args.summary, "transaction_id": args.transaction_id, "auto": args.auto})
    state.update({"schema_version": RUNTIME_VERSION, "auto_archive_dates": sorted(auto_dates)[-90:], "history": history[-180:]})
    save_json(state_path, state)
    emit({"ok": True, "dates": target_dates, "auto_archive_dates": state["auto_archive_dates"]})
    return 0


def command_health(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    paths = ensure_runtime(vault)
    with state_guard(vault):
        lock_state = load_json(paths["locks"], {"locks": []})
        locks = lock_state.get("locks", []) if isinstance(lock_state, dict) else []
        active, expired = prune_expired_locks(vault, locks)
        save_json(paths["locks"], {"schema_version": RUNTIME_VERSION, "locks": active})
    transactions = []
    for manifest_path in paths["transactions"].glob("*/manifest.json"):
        manifest = load_json(manifest_path, {})
        if isinstance(manifest, dict):
            transactions.append({key: manifest.get(key) for key in ("transaction_id", "owner", "mode", "stage", "created_at", "updated_at", "completed_at")})
    open_transactions = [item for item in transactions if item["stage"] not in {"COMMITTED", "ROLLED_BACK"}]
    emit({
        "ok": not open_transactions,
        "runtime": str(paths["root"]),
        "open_transactions": open_transactions,
        "active_locks": active,
        "expired_locks_reclaimed": expired,
    })
    return 0 if not open_transactions else 3


def command_cleanup_runtime(args: argparse.Namespace) -> int:
    vault = resolve_vault(args.vault)
    root = ensure_runtime(vault)["transactions"]
    cutoff = now_local() - timedelta(days=RETENTION_DAYS)
    removed = []
    retained = []
    for manifest_path in root.glob("*/manifest.json"):
        manifest = load_json(manifest_path, {})
        stage = manifest.get("stage") if isinstance(manifest, dict) else None
        completed = manifest.get("completed_at") if isinstance(manifest, dict) else None
        if stage in {"COMMITTED", "ROLLED_BACK"} and completed and parse_datetime(str(completed)) < cutoff:
            shutil.rmtree(manifest_path.parent)
            removed.append(manifest_path.parent.name)
        else:
            retained.append({"transaction_id": manifest_path.parent.name, "stage": stage, "completed_at": completed})
    work_removed = []
    work_root = ensure_runtime(vault)["work_quarantine"]
    for manifest_path in work_root.glob("*/manifest.json"):
        manifest = load_json(manifest_path, {})
        completed = manifest.get("completed_at") if isinstance(manifest, dict) else None
        if completed and parse_datetime(str(completed)) < cutoff:
            shutil.rmtree(manifest_path.parent)
            work_removed.append(manifest_path.parent.name)
    emit({"ok": True, "retention_days": RETENTION_DAYS, "removed_transactions": removed, "removed_work_quarantine": work_removed, "retained": retained})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a concurrent three-layer Obsidian engineering Vault.")
    parser.add_argument("--vault", help="Vault 路径，优先于环境变量和配置文件")
    parser.add_argument("--config", help="本地 JSON 配置文件")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show-config").set_defaults(func=lambda args: (emit(args.settings) or 0))
    sub.add_parser("init").set_defaults(func=command_init)
    health = sub.add_parser("health")
    health.set_defaults(func=command_health)
    check = sub.add_parser("check-archive")
    check.add_argument("--manual", action="store_true")
    check.set_defaults(func=command_check_archive)
    search = sub.add_parser("search-index")
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=8)
    search.set_defaults(func=command_search_index)
    candidates = sub.add_parser("candidate-search")
    candidates.add_argument("--query", required=True)
    candidates.add_argument("--limit", type=int, default=20)
    candidates.set_defaults(func=command_candidate_search)
    new_note = sub.add_parser("new-note")
    new_note.add_argument("--title", required=True)
    new_note.add_argument("--kind", choices=("experience", "source-note", "decision", "preference", "daily-inbox"), default="experience")
    new_note.add_argument("--project", default="")
    new_note.add_argument("--date")
    new_note.add_argument("--owner", required=True)
    new_note.set_defaults(func=command_new_note)
    proposal = sub.add_parser("propose-index-update")
    proposal.add_argument("--index", required=True)
    proposal.add_argument("--source", required=True)
    proposal.add_argument("--reason", required=True)
    proposal.add_argument("--owner", required=True)
    proposal.set_defaults(func=command_propose_index_update)
    heartbeat = sub.add_parser("heartbeat")
    heartbeat.add_argument("--owner", required=True)
    heartbeat.set_defaults(func=command_heartbeat)
    release = sub.add_parser("release-lock")
    release.add_argument("--owner", required=True)
    release.add_argument("--lock-id")
    release.set_defaults(func=command_release_lock)
    safe_write = sub.add_parser("safe-write")
    safe_write.add_argument("--owner", required=True)
    safe_write.add_argument("--path", required=True)
    safe_write.add_argument("--expected-sha256", required=True, help="Use NONE for a new file")
    safe_write.add_argument("--input", required=True)
    safe_write.set_defaults(func=command_safe_write)
    safe_delete = sub.add_parser("safe-delete")
    safe_delete.add_argument("--owner", required=True)
    safe_delete.add_argument("--path", required=True)
    safe_delete.add_argument("--expected-sha256", required=True)
    safe_delete.set_defaults(func=command_safe_delete)
    scan = sub.add_parser("scan")
    scan.add_argument("--output")
    scan.set_defaults(func=command_scan)
    begin = sub.add_parser("begin-transaction")
    begin.add_argument("--mode", choices=("archive",), required=True)
    begin.add_argument("--owner", required=True)
    begin.add_argument("--path", action="append", default=[])
    begin.set_defaults(func=command_begin_transaction)
    extend = sub.add_parser("extend-transaction")
    extend.add_argument("--transaction-id", required=True)
    extend.add_argument("--owner", required=True)
    extend.add_argument("--path", action="append", required=True)
    extend.set_defaults(func=command_extend_transaction)
    atomic = sub.add_parser("atomic-write")
    atomic.add_argument("--transaction-id", required=True)
    atomic.add_argument("--owner", required=True)
    atomic.add_argument("--path", required=True)
    atomic.add_argument("--input", required=True)
    atomic.set_defaults(func=command_atomic_write)
    accept = sub.add_parser("accept-external")
    accept.add_argument("--transaction-id", required=True)
    accept.add_argument("--owner", required=True)
    accept.add_argument("--path", required=True)
    accept.set_defaults(func=command_accept_external)
    quarantine = sub.add_parser("quarantine")
    quarantine.add_argument("--transaction-id", required=True)
    quarantine.add_argument("--owner", required=True)
    quarantine.add_argument("--path", required=True)
    quarantine.set_defaults(func=command_quarantine)
    advance = sub.add_parser("advance-transaction")
    advance.add_argument("--transaction-id", required=True)
    advance.add_argument("--owner", required=True)
    advance.add_argument("--stage", choices=("MUTATING", "VALIDATED", "COMMITTED"), required=True)
    advance.set_defaults(func=command_advance_transaction)
    rollback = sub.add_parser("rollback-transaction")
    rollback.add_argument("--transaction-id", required=True)
    rollback.add_argument("--owner", required=True)
    rollback.set_defaults(func=command_rollback_transaction)
    mark = sub.add_parser("mark-archived")
    mark.add_argument("--date", action="append", required=True)
    mark.add_argument("--transaction-id", required=True)
    mark.add_argument("--summary", default="")
    mark.add_argument("--auto", action="store_true")
    mark.set_defaults(func=command_mark_archived)
    sub.add_parser("cleanup-runtime").set_defaults(func=command_cleanup_runtime)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.settings = configure(args)
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError) as error:
        emit({"ok": False, "error": str(error)})
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
