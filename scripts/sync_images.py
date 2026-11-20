#!/usr/bin/env python3
"""
通用 Docker 镜像转存脚本（仅使用 Python 标准库 + skopeo CLI）
"""

from __future__ import annotations

import argparse
import concurrent.futures
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import List, Optional, Tuple


LOG_LOCK = threading.Lock()
LOG_FILE_HANDLE = None

REGISTRY_ALIASES = {
    "tencent": "ccr.ccs.tencentyun.com",
    "ccr": "ccr.ccs.tencentyun.com",
    "huawei": "swr.cn-east-3.myhuaweicloud.com",
    "swr": "swr.cn-east-3.myhuaweicloud.com",
}


def log(level: str, message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{timestamp}] [{level:<7}] {message}"
    with LOG_LOCK:
        print(line, flush=True)
        if LOG_FILE_HANDLE is not None:
            LOG_FILE_HANDLE.write(line + "\n")
            LOG_FILE_HANDLE.flush()


@dataclass
class ParsedImage:
    raw: str
    registry: str
    repo_path: str
    image_name: str
    tag: str
    digest: str

    @property
    def source_ref(self) -> str:
        base = f"{self.registry}/{self.repo_path}"
        if self.digest:
            return f"{base}@{self.digest}"
        return f"{base}:{self.tag}"

    @property
    def target_tag(self) -> str:
        if self.tag:
            return self.tag
        if self.digest:
            return self.digest.replace(":", "-")[:32]
        return "latest"


@dataclass
class SyncResult:
    source_raw: str
    source_ref: str
    target_ref: str
    status: str         # SUCCESS | SKIPPED | FAILED | DRY_RUN
    attempts: int
    duration_sec: float
    message: str = ""


def parse_image_ref(raw_ref: str) -> ParsedImage:
    ref = raw_ref.strip()
    digest = ""
    if "@" in ref:
        ref, digest = ref.split("@", 1)

    last_slash = ref.rfind("/")
    last_colon = ref.rfind(":")
    if last_colon > last_slash:
        name_part = ref[:last_colon]
        tag = ref[last_colon + 1:]
    else:
        name_part = ref
        tag = "" if digest else "latest"

    parts = name_part.split("/")
    if len(parts) == 1:
        registry = "docker.io"
        repo_path = f"library/{parts[0]}"
    elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        registry = parts[0]
        repo_path = "/".join(parts[1:])
    else:
        registry = "docker.io"
        repo_path = name_part

    image_name = repo_path.split("/")[-1]
    return ParsedImage(
        raw=raw_ref,
        registry=registry,
        repo_path=repo_path,
        image_name=image_name,
        tag=tag,
        digest=digest,
    )


def build_target_ref(
    parsed: ParsedImage,
    target_registry: str,
    target_namespace: str,
    naming_style: str,
) -> str:
    if naming_style == "a":
        return f"{target_registry}/{target_namespace}/{parsed.image_name}:{parsed.target_tag}"
    if naming_style == "b":
        return f"{target_registry}/{target_namespace}/{parsed.registry}/{parsed.repo_path}:{parsed.target_tag}"
    raise ValueError(f"不支持的命名风格: {naming_style}")


def collect_images(image_file: str, images_inline: str) -> List[str]:
    collected: List[str] = []
    file_path = image_file.strip()
    inline_str = images_inline.strip()

    if file_path:
        path_obj = Path(file_path)
        if path_obj.is_file():
            log("INFO", f"读取镜像清单文件: {file_path}")
            with path_obj.open("r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.split("#", 1)[0].strip()
                    if line:
                        collected.append(line)
        else:
            if inline_str and file_path == "images.txt":
                log("WARN", f"默认清单文件 '{file_path}' 不存在，将仅同步手动输入的镜像列表。")
            else:
                raise FileNotFoundError(f"指定的镜像清单文件不存在: {file_path}")

    if inline_str:
        log("INFO", "解析手动输入的镜像列表...")
        for item in re.split(r"[,;\r\n]+", inline_str):
            cleaned = item.strip()
            if cleaned:
                collected.append(cleaned)

    seen = set()
    unique_images: List[str] = []
    for img in collected:
        if img not in seen:
            seen.add(img)
            unique_images.append(img)
    return unique_images


def filter_images(images: List[str], image_filter: str) -> List[str]:
    raw_filter = image_filter.strip()
    if not raw_filter:
        return images
    keywords = [k.strip().lower() for k in re.split(r"[,;]+", raw_filter) if k.strip()]
    if not keywords:
        return images
    matched = [img for img in images if any(kw in img.lower() for kw in keywords)]
    log("INFO", f"过滤条件 {keywords}: 从 {len(images)} 个镜像中匹配到 {len(matched)} 个。")
    return matched


def normalize_arch(arch_input: str) -> Tuple[str, str, str]:
    val = arch_input.strip().lower() or "amd64"
    if val == "all":
        return "all", "", ""
    if "/" in val:
        os_name, arch_name = val.split("/", 1)
    else:
        os_name, arch_name = "linux", val

    alias_map = {"x86_64": "amd64", "aarch64": "arm64"}
    arch_name = alias_map.get(arch_name, arch_name)
    return "single", os_name, arch_name


def resolve_target_registry_and_namespace(
    target_registry: str,
    target_namespace: str,
) -> Tuple[str, str]:
    reg_raw = re.sub(r"^https?://", "", target_registry.strip()).strip("/")
    reg = REGISTRY_ALIASES.get(reg_raw.lower(), reg_raw)
    if not reg:
        raise ValueError("目标同步地址不能为空。")

    ns = target_namespace.strip().strip("/")
    if not ns:
        raise ValueError("目标同步仓库/命名空间不能为空。")

    return reg, ns


def resolve_credentials(target_registry: str) -> Tuple[str, str]:
    reg_lower = target_registry.lower()
    if "tencentyun.com" in reg_lower:
        user = os.getenv("TENCENT_REGISTRY_USER") or os.getenv("TARGET_REGISTRY_USER", "")
        pwd = os.getenv("TENCENT_REGISTRY_PASSWORD") or os.getenv("TARGET_REGISTRY_PASSWORD", "")
        hint = "TENCENT_REGISTRY_USER / TENCENT_REGISTRY_PASSWORD"
    elif "myhuaweicloud.com" in reg_lower:
        user = os.getenv("HUAWEI_REGISTRY_USER") or os.getenv("TARGET_REGISTRY_USER", "")
        pwd = os.getenv("HUAWEI_REGISTRY_PASSWORD") or os.getenv("TARGET_REGISTRY_PASSWORD", "")
        hint = "HUAWEI_REGISTRY_USER / HUAWEI_REGISTRY_PASSWORD"
    else:
        user = os.getenv("CUSTOM_REGISTRY_USER") or os.getenv("TARGET_REGISTRY_USER", "")
        pwd = os.getenv("CUSTOM_REGISTRY_PASSWORD") or os.getenv("TARGET_REGISTRY_PASSWORD", "")
        hint = "CUSTOM_REGISTRY_USER / CUSTOM_REGISTRY_PASSWORD (或 TARGET_REGISTRY_USER / TARGET_REGISTRY_PASSWORD)"

    if not user or not pwd:
        raise RuntimeError(f"未检测到目标仓库 '{target_registry}' 的认证信息，请在 GitHub Secrets 中配置 {hint}。")
    return user, pwd


def skopeo_login(authfile: str, registry: str, username: str, password: str, tls_verify: bool) -> None:
    cmd = [
        "skopeo",
        "login",
        "--authfile",
        authfile,
        "--username",
        username,
        "--password-stdin",
    ]
    if not tls_verify:
        cmd.append("--tls-verify=false")
    cmd.append(registry)

    proc = subprocess.run(cmd, input=password, text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"登录镜像仓库 {registry} 失败: {err}")
    log("INFO", f"成功登录仓库: {registry}")


def setup_authfile(target_registry: str, tls_verify: bool) -> str:
    fd, authfile = tempfile.mkstemp(prefix="skopeo-auth-", suffix=".json")
    os.close(fd)
    os.chmod(authfile, 0o600)
    with open(authfile, "w", encoding="utf-8") as f:
        json.dump({"auths": {}}, f)

    dest_user, dest_pwd = resolve_credentials(target_registry)
    skopeo_login(authfile, target_registry, dest_user, dest_pwd, tls_verify)

    dh_user = os.getenv("DOCKERHUB_USER", "").strip()
    dh_token = os.getenv("DOCKERHUB_TOKEN", "").strip()
    if dh_user and dh_token:
        skopeo_login(authfile, "docker.io", dh_user, dh_token, tls_verify=True)

    src_host = os.getenv("SOURCE_REGISTRY_HOST", "").strip()
    src_user = os.getenv("SOURCE_REGISTRY_USER", "").strip()
    src_pwd = os.getenv("SOURCE_REGISTRY_PASSWORD", "").strip()
    if src_host and src_user and src_pwd:
        skopeo_login(authfile, src_host, src_user, src_pwd, tls_verify)

    return authfile


def get_remote_digest(
    image_ref: str,
    authfile: str,
    arch_mode: str,
    os_name: str,
    arch_name: str,
    tls_verify: bool,
) -> Optional[str]:
    cmd = ["skopeo", "--insecure-policy", "inspect", "--raw", "--authfile", authfile]
    if not tls_verify:
        cmd.append("--tls-verify=false")
    cmd.append(f"docker://{image_ref}")

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=45)
    except Exception:
        return None

    if proc.returncode != 0 or not proc.stdout:
        return None

    raw_bytes = proc.stdout
    try:
        manifest = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()

    if isinstance(manifest, dict) and isinstance(manifest.get("manifests"), list):
        if arch_mode == "all":
            return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
        for item in manifest["manifests"]:
            plat = item.get("platform", {})
            if plat.get("architecture") == arch_name and plat.get("os", "linux") == os_name:
                return item.get("digest")
        return None

    return "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


def sync_single_image(
    idx: int,
    total: int,
    parsed: ParsedImage,
    target_ref: str,
    authfile: str,
    arch_mode: str,
    os_name: str,
    arch_name: str,
    sync_mode: str,
    tls_verify: bool,
    max_retries: int,
    timeout_sec: int,
) -> SyncResult:
    start_ts = time.monotonic()
    prefix = f"[{idx}/{total}] {parsed.source_ref} -> {target_ref}"

    if sync_mode == "dry-run":
        log("DRY-RUN", prefix)
        return SyncResult(
            source_raw=parsed.raw,
            source_ref=parsed.source_ref,
            target_ref=target_ref,
            status="DRY_RUN",
            attempts=0,
            duration_sec=round(time.monotonic() - start_ts, 2),
            message="Dry-run preview",
        )

    if sync_mode == "incremental":
        src_digest = get_remote_digest(parsed.source_ref, authfile, arch_mode, os_name, arch_name, tls_verify)
        if src_digest:
            dst_digest = get_remote_digest(target_ref, authfile, arch_mode, os_name, arch_name, tls_verify)
            if dst_digest and src_digest == dst_digest:
                log("SKIP", f"{prefix} (Digest 一致: {src_digest[:19]}...)")
                return SyncResult(
                    source_raw=parsed.raw,
                    source_ref=parsed.source_ref,
                    target_ref=target_ref,
                    status="SKIPPED",
                    attempts=0,
                    duration_sec=round(time.monotonic() - start_ts, 2),
                    message=f"Up-to-date ({src_digest[:19]}...)",
                )

    cmd = ["skopeo", "--insecure-policy"]
    if arch_mode == "single":
        cmd.extend(["--override-os", os_name, "--override-arch", arch_name])
    cmd.extend(["copy", "--authfile", authfile])
    if arch_mode == "all":
        cmd.append("--all")
    if not tls_verify:
        cmd.extend(["--src-tls-verify=false", "--dest-tls-verify=false"])
    cmd.extend([f"docker://{parsed.source_ref}", f"docker://{target_ref}"])

    last_error = ""
    total_attempts = 1 + max_retries

    for attempt in range(1, total_attempts + 1):
        if attempt == 1:
            log("INFO", f"{prefix} 开始同步 (尝试 {attempt}/{total_attempts})")
        else:
            backoff = min(5 * (2 ** (attempt - 2)), 30)
            log("WARN", f"{prefix} 第 {attempt - 1}/{max_retries} 次重试（等待 {backoff}s）...")
            time.sleep(backoff)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if proc.returncode == 0:
                elapsed = round(time.monotonic() - start_ts, 2)
                log("SUCCESS", f"{prefix} 同步成功（耗时 {elapsed}s）")
                return SyncResult(
                    source_raw=parsed.raw,
                    source_ref=parsed.source_ref,
                    target_ref=target_ref,
                    status="SUCCESS",
                    attempts=attempt,
                    duration_sec=elapsed,
                )
            last_error = (proc.stderr or proc.stdout or f"Exit code {proc.returncode}").strip()
        except subprocess.TimeoutExpired:
            last_error = f"执行超时（>{timeout_sec}s）"
        except Exception as exc:
            last_error = str(exc)

        short_err = " | ".join(last_error.splitlines()[-3:])
        log("WARN", f"{prefix} 尝试 {attempt}/{total_attempts} 失败: {short_err}")

    elapsed = round(time.monotonic() - start_ts, 2)
    final_short_err = " | ".join(last_error.splitlines()[-3:])
    log("ERROR", f"{prefix} 重试 {max_retries} 次后仍失败，已记录并跳过。原因: {final_short_err}")
    return SyncResult(
        source_raw=parsed.raw,
        source_ref=parsed.source_ref,
        target_ref=target_ref,
        status="FAILED",
        attempts=total_attempts,
        duration_sec=elapsed,
        message=final_short_err,
    )


def write_github_summary(
    results: List[SyncResult],
    target_registry: str,
    target_namespace: str,
    naming_style: str,
    arch_display: str,
    concurrency: int,
    image_filter: str,
    sync_mode: str,
    total_duration: float,
) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return

    counts = {
        "SUCCESS": sum(1 for r in results if r.status == "SUCCESS"),
        "SKIPPED": sum(1 for r in results if r.status == "SKIPPED"),
        "FAILED": sum(1 for r in results if r.status == "FAILED"),
        "DRY_RUN": sum(1 for r in results if r.status == "DRY_RUN"),
    }

    lines = [
        "## 🐳 Docker 镜像转存报告",
        "",
        "| 配置项 | 值 |",
        "| --- | --- |",
        f"| 目标地址 | `{target_registry}` |",
        f"| 目标仓库/命名空间 | `{target_namespace}` |",
        f"| 命名风格 | `{naming_style}` |",
        f"| 同步架构 | `{arch_display}` |",
        f"| 并发数 | `{concurrency}` |",
        f"| 过滤关键字 | `{image_filter or '无'}` |",
        f"| 同步模式 | `{sync_mode}` |",
        f"| 总耗时 | `{round(total_duration, 2)}s` |",
        "",
        f"**统计**：总数 `{len(results)}` ｜ 成功 `{counts['SUCCESS']}` ｜ 跳过 `{counts['SKIPPED']}` ｜ 失败 `{counts['FAILED']}` ｜ 预演 `{counts['DRY_RUN']}`",
        "",
        "| # | 源镜像 | 目标镜像 | 状态 | 尝试次数 | 耗时(s) | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    status_icon = {
        "SUCCESS": "✅ SUCCESS",
        "SKIPPED": "⏭️ SKIPPED",
        "FAILED": "❌ FAILED",
        "DRY_RUN": "🔍 DRY_RUN",
    }

    for i, r in enumerate(results, 1):
        safe_msg = r.message.replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(
            f"| {i} | `{r.source_ref}` | `{r.target_ref}` | {status_icon.get(r.status, r.status)} | {r.attempts} | {r.duration_sec} | {safe_msg} |"
        )

    failed_list = [r.source_raw for r in results if r.status == "FAILED"]
    if failed_list:
        lines.extend([
            "",
            "### ❌ 失败镜像清单（可直接复制粘贴到 `images` 参数补跑）",
            "```text",
            ",".join(failed_list),
            "```",
        ])

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generic Docker Image Sync Service")
    parser.add_argument("--image-file", default="images.txt")
    parser.add_argument("--images", default="")
    parser.add_argument("--target-registry", required=True)
    parser.add_argument("--target-namespace", required=True)
    parser.add_argument("--naming-style", choices=["a", "b"], default="a")
    parser.add_argument("--arch", default="amd64")
    parser.add_argument("--concurrency", default="1")
    parser.add_argument("--image-filter", default="")
    parser.add_argument("--sync-mode", choices=["incremental", "force", "dry-run"], default="incremental")
    parser.add_argument("--tls-verify", default="true")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--log-file", default="sync.log")
    parser.add_argument("--report-file", default="sync-report.json")
    parser.add_argument("--failed-file", default="failed-images.txt")
    return parser.parse_args()


def main() -> int:
    global LOG_FILE_HANDLE
    args = parse_args()
    LOG_FILE_HANDLE = open(args.log_file, "w", encoding="utf-8")
    overall_start = time.monotonic()
    authfile: Optional[str] = None

    try:
        target_registry, target_namespace = resolve_target_registry_and_namespace(
            args.target_registry,
            args.target_namespace,
        )
        arch_mode, os_name, arch_name = normalize_arch(args.arch)
        arch_display = "all" if arch_mode == "all" else f"{os_name}/{arch_name}"

        try:
            concurrency = int(args.concurrency)
        except ValueError:
            raise ValueError(f"并发数必须为整数，当前输入: {args.concurrency}")
        if concurrency < 1:
            concurrency = 1
        elif concurrency > 16:
            log("WARN", f"并发数 {concurrency} 过高，已自动限制为 16 以避免触发仓库限流。")
            concurrency = 16

        tls_verify = str(args.tls_verify).strip().lower() in {"true", "1", "yes"}

        if target_registry == "ccr.ccs.tencentyun.com" and args.naming_style == "b":
            log("WARN", "检测到向腾讯云 CCR 使用风格 b（多级路径），请确认目标仓库实例支持多级路径（个人版通常仅支持风格 a）。")

        log("INFO", f"目标地址: {target_registry} | 目标仓库: {target_namespace} | 风格: {args.naming_style} | 架构: {arch_display} | 并发: {concurrency} | 模式: {args.sync_mode}")

        raw_images = collect_images(args.image_file, args.images)
        filtered_images = filter_images(raw_images, args.image_filter)

        if not filtered_images:
            log("WARN", "没有需要同步的镜像，任务结束。")
            write_github_summary([], target_registry, target_namespace, args.naming_style, arch_display, concurrency, args.image_filter, args.sync_mode, 0.0)
            return 0

        tasks: List[Tuple[ParsedImage, str]] = []
        target_seen = {}
        for raw in filtered_images:
            parsed = parse_image_ref(raw)
            dst = build_target_ref(parsed, target_registry, target_namespace, args.naming_style)
            if dst in target_seen:
                log("WARN", f"目标镜像名冲突: '{raw}' 与 '{target_seen[dst]}' 均映射到 '{dst}'，请留意风格 a 下的同名覆盖风险。")
            target_seen[dst] = raw
            tasks.append((parsed, dst))

        if args.sync_mode != "dry-run":
            authfile = setup_authfile(target_registry, tls_verify)

        total = len(tasks)
        results: List[Optional[SyncResult]] = [None] * total

        if concurrency == 1 or args.sync_mode == "dry-run":
            for idx, (parsed, dst) in enumerate(tasks, 1):
                results[idx - 1] = sync_single_image(
                    idx, total, parsed, dst, authfile or "",
                    arch_mode, os_name, arch_name, args.sync_mode,
                    tls_verify, args.max_retries, args.timeout,
                )
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                future_to_idx = {
                    executor.submit(
                        sync_single_image,
                        idx, total, parsed, dst, authfile or "",
                        arch_mode, os_name, arch_name, args.sync_mode,
                        tls_verify, args.max_retries, args.timeout,
                    ): idx - 1
                    for idx, (parsed, dst) in enumerate(tasks, 1)
                }
                for future in concurrent.futures.as_completed(future_to_idx):
                    pos = future_to_idx[future]
                    try:
                        results[pos] = future.result()
                    except Exception as exc:
                        parsed, dst = tasks[pos]
                        log("ERROR", f"[{pos + 1}/{total}] 未知异常: {exc}")
                        results[pos] = SyncResult(
                            source_raw=parsed.raw,
                            source_ref=parsed.source_ref,
                            target_ref=dst,
                            status="FAILED",
                            attempts=1,
                            duration_sec=0.0,
                            message=str(exc),
                        )

        final_results = [r for r in results if r is not None]
        total_duration = round(time.monotonic() - overall_start, 2)

        with open(args.report_file, "w", encoding="utf-8") as rf:
            json.dump(
                {
                    "summary": {
                        "total": len(final_results),
                        "success": sum(1 for r in final_results if r.status == "SUCCESS"),
                        "skipped": sum(1 for r in final_results if r.status == "SKIPPED"),
                        "failed": sum(1 for r in final_results if r.status == "FAILED"),
                        "dry_run": sum(1 for r in final_results if r.status == "DRY_RUN"),
                        "duration_sec": total_duration,
                    },
                    "results": [asdict(r) for r in final_results],
                },
                rf,
                indent=2,
                ensure_ascii=False,
            )

        failed_images = [r.source_raw for r in final_results if r.status == "FAILED"]
        if failed_images:
            with open(args.failed_file, "w", encoding="utf-8") as ff:
                ff.write("\n".join(failed_images) + "\n")

        write_github_summary(
            final_results,
            target_registry,
            target_namespace,
            args.naming_style,
            arch_display,
            concurrency,
            args.image_filter,
            args.sync_mode,
            total_duration,
        )

        log("INFO", f"全部同步流程结束！总计 {len(final_results)}，失败 {len(failed_images)}，总耗时 {total_duration}s。")
        return 0

    except Exception as exc:
        log("FATAL", f"初始化或配置阶段异常: {exc}")
        return 1
    finally:
        if authfile and os.path.exists(authfile):
            os.remove(authfile)
        if LOG_FILE_HANDLE is not None:
            LOG_FILE_HANDLE.close()


if __name__ == "__main__":
    sys.exit(main())
