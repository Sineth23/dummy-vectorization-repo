#!/usr/bin/env python3
# detect_changes_task.py (reverted simple version)
#
# Computes Git changes between base_commit and current branch HEAD.
# Writes report JSON to:
#   s3://<bucket>/cloned-repos/<repo>/_changes/<YYYYMMDDT%H%M>__<base>__<head>.json
#
# Required env:
#   DATA_BUCKET
#   REPO_URL
#
# Optional env:
#   CLONED_REPOS_PREFIX (default: cloned-repos)
#   REPO_NAME (derived from repo_url if missing)
#   BRANCH (default: main)
#   GITHUB_TOKEN
#   BASE_COMMIT (if empty, uses _clone_manifest.json head_commit)
#
# NOTE: This task does NOT modify S3 repo snapshot.

import os
import json
import tempfile
import shutil
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

TORONTO_TZ = ZoneInfo("America/Toronto")

DATA_BUCKET = os.environ["DATA_BUCKET"]
CLONED_REPOS_PREFIX = os.environ.get("CLONED_REPOS_PREFIX", "cloned-repos").rstrip("/")

REPO_URL = os.environ.get("REPO_URL", "")
BRANCH = os.environ.get("BRANCH", "main")
REPO_NAME = os.environ.get("REPO_NAME", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASE_COMMIT_ENV = (os.environ.get("BASE_COMMIT") or "").strip()


def _run(cmd, cwd=None) -> str:
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n"
            f"cwd={cwd}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
    return (p.stdout or "").strip()


def _derive_repo_name(repo_url: str) -> str:
    p = urlparse(repo_url)
    base = os.path.basename(p.path.rstrip("/"))
    return base[:-4] if base.endswith(".git") else base


def _inject_token(repo_url: str, token: str) -> str:
    if not token:
        return repo_url
    parsed = urlparse(repo_url)
    host = parsed.netloc
    if host.startswith("api.github.com"):
        host = "github.com"
    if "@" in host:
        return urlunparse(parsed._replace(netloc=host))
    return urlunparse(parsed._replace(netloc=f"{token}@{host}"))


def _now_est_compact() -> str:
    return datetime.now(TORONTO_TZ).strftime("%Y%m%dT%H%M")


def _now_utc_iso_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _s3_get_json(bucket: str, key: str):
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        raise


def _s3_put_json(bucket: str, key: str, payload: dict):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )


def _parse_name_status(diff_text: str):
    changes = []
    for line in (diff_text or "").splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        status = parts[0]
        code = status[:1]

        if code in ("A", "M", "D", "T") and len(parts) >= 2:
            typ_map = {"A": "added", "M": "modified", "D": "deleted", "T": "type-changed"}
            changes.append({"type": typ_map[code], "file_path": parts[1], "status": status})
        elif code in ("R", "C") and len(parts) >= 3:
            typ_map = {"R": "rename", "C": "copy"}
            changes.append({"type": typ_map[code], "old_path": parts[1], "new_path": parts[2], "status": status})
        else:
            changes.append({"type": "unknown", "raw": parts})
    return changes


def _summarize(changes):
    summary = {
        "total": len(changes),
        "added": 0,
        "modified": 0,
        "deleted": 0,
        "rename": 0,
        "copy": 0,
        "type-changed": 0,
        "unknown": 0,
    }

    add_set, update_set, delete_set = set(), set(), set()

    for c in changes:
        t = c.get("type", "unknown")
        if t not in summary:
            t = "unknown"
        summary[t] += 1

        if t == "added":
            add_set.add(c["file_path"])
        elif t in ("modified", "type-changed"):
            update_set.add(c.get("file_path", ""))
        elif t == "deleted":
            delete_set.add(c["file_path"])
        elif t == "rename":
            delete_set.add(c["old_path"])
            add_set.add(c["new_path"])
        elif t == "copy":
            add_set.add(c["new_path"])

    apply_plan = {
        "add": sorted([p for p in add_set if p]),
        "update": sorted([p for p in update_set if p]),
        "delete": sorted([p for p in delete_set if p]),
    }
    return summary, apply_plan


def main():
    if not REPO_URL:
        raise RuntimeError("REPO_URL is required")

    repo_name = REPO_NAME.strip() or _derive_repo_name(REPO_URL)
    repo_prefix = f"{CLONED_REPOS_PREFIX}/{repo_name}".rstrip("/")

    manifest_key = f"{repo_prefix}/_clone_manifest.json"
    manifest = _s3_get_json(DATA_BUCKET, manifest_key)

    base_commit = BASE_COMMIT_ENV
    if not base_commit and manifest:
        base_commit = (manifest.get("head_commit") or "").strip()

    # Fetch latest head commit for branch using a minimal bare repo
    tmp = tempfile.mkdtemp(prefix="autodoc-detect-")
    bare = os.path.join(tmp, "bare")

    try:
        os.makedirs(bare, exist_ok=True)
        _run(["git", "init", "--bare"], cwd=bare)

        remote = _inject_token(REPO_URL, GITHUB_TOKEN)
        _run(["git", "remote", "add", "origin", remote], cwd=bare)

        # Fetch just the branch tip (no blobs needed)
        _run(["git", "fetch", "--prune", "--filter=blob:none", "origin", BRANCH], cwd=bare)
        head_commit = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=bare).strip()

        generated_at_est = _now_est_compact()
        generated_at_utc = _now_utc_iso_z()

        if not base_commit or base_commit == head_commit:
            changes = []
        else:
            diff_text = _run(["git", "diff", "--name-status", "-M", "-C", f"{base_commit}..{head_commit}"], cwd=bare)
            changes = _parse_name_status(diff_text)

        summary, apply_plan = _summarize(changes)

        report_key = f"{repo_prefix}/_changes/{generated_at_est}__{(base_commit or 'NONE')}__{(head_commit or 'NONE')}.json"
        report_uri = f"s3://{DATA_BUCKET}/{report_key}"

        report = {
            "ok": True,
            "kind": "detect_changes_report",
            "repo_url": REPO_URL,
            "repo_name": repo_name,
            "branch": BRANCH,
            "base_commit": base_commit,
            "head_commit": head_commit,
            "generated_at_est": generated_at_est,
            "generated_at_utc": generated_at_utc,
            "total_changed_files": summary["total"],
            "summary": summary,
            "apply_plan": apply_plan,
            "changes": changes,
            "report": {"s3_bucket": DATA_BUCKET, "s3_key": report_key, "s3_uri": report_uri},
            "manifest_used": {
                "s3_uri": f"s3://{DATA_BUCKET}/{manifest_key}",
                "head_commit": (manifest or {}).get("head_commit"),
            } if manifest else None,
        }

        _s3_put_json(DATA_BUCKET, report_key, report)
        print(json.dumps(report))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
