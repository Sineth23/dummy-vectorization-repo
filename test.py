# vectorizestoreFAST.py
import os
import uuid
import logging
import time
from typing import Iterable, List, Dict, Optional, Tuple, Union, Any

import numpy as np
import deeplake
from deeplake.util.exceptions import DatasetHandlerError
try:
    from deeplake.util.exceptions import DatasetCorruptError
except Exception:
    DatasetCorruptError = Exception

from git import Repo
from sentence_transformers import SentenceTransformer
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import torch

from chunking import SmartChunker
from config import VectorizationConfig
from speed_utils import set_perf_env, path_key, iter_source_files, batched_encode

import json
PROGRESS_JSON = os.environ.get("PROGRESS_JSON") == "1"

def _event(obj: dict):
    if PROGRESS_JSON:
        print(json.dumps(obj, ensure_ascii=False), flush=True)

# ---------------------------------------------------------------------
# Logging & perf clamps
# ---------------------------------------------------------------------
logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
set_perf_env()

dataset_lock = Lock()
git_lock = Lock()

# ---------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------
def _filter_by_year(commits, year_filter: Optional[Union[int, Tuple[int, int]]]):
    if year_filter is None:
        return commits

    def _keep(c):
        y = c.committed_datetime.year
        if isinstance(year_filter, int):
            return y == year_filter
        lo, hi = year_filter
        return lo <= y <= hi

    return [c for c in commits if _keep(c)]

def _commit_to_meta(c) -> Dict[str, Any]:
    return {
        "commit_hash": c.hexsha,
        "author_name": c.author.name if c.author else None,
        "author_email": c.author.email if c.author else None,
        "commit_date": c.committed_datetime.isoformat(),
        "commit_message": (c.message or "").strip(),
    }

def get_latest_commit_meta(
    repo: Repo,
    rel_path: str,
    year_filter: Optional[Union[int, Tuple[int, int]]] = None,
) -> Dict[str, Any]:
    """
    Returns the *latest* commit meta for this file path (optionally year-filtered).
    This is used for CODE chunks (we store code once, not once-per-commit).
    """
    try:
        with git_lock:
            commits = list(repo.iter_commits("--all", paths=rel_path))
        commits = _filter_by_year(commits, year_filter)
        return _commit_to_meta(commits[0]) if commits else {}
    except Exception as e:
        logging.warning(f"[gitmeta] latest meta failed for {rel_path}: {e}")
        return {}

def get_file_diffs_with_meta(
    repo: Repo,
    rel_path: str,
    year_filter: Optional[Union[int, Tuple[int, int]]] = None,
) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Returns a list of (diff_text, commit_meta) where commit_meta matches the commit
    that produced that diff.
    """
    out: List[Tuple[str, Dict[str, Any]]] = []
    try:
        with git_lock:
            commits = list(repo.iter_commits("--all", paths=rel_path))
        commits = _filter_by_year(commits, year_filter)

        for c in commits:
            if not c.parents:
                continue
            try:
                # Diff for THIS commit affecting THIS file
                with git_lock:
                    diff_text = repo.git.diff(c.parents[0], c, rel_path)
                if diff_text:
                    out.append((diff_text, _commit_to_meta(c)))
            except Exception as e:
                logging.warning(f"[diffs] failed for {rel_path}@{getattr(c,'hexsha','?')}: {e}")

    except Exception as e:
        logging.warning(f"[diffs] list commits failed for {rel_path}: {e}")

    return out

# ---------------------------------------------------------------------
# Dataset helpers (ONLY batched extends; no .append anywhere)
# ---------------------------------------------------------------------
def _append_many_fast(ds: deeplake.Dataset, records: List[Dict]) -> None:
    if not records:
        return

    ids = [r["id"] for r in records]
    embs = np.stack([r["embedding"] for r in records]).astype(np.float32, copy=False)
    texts = [r["text"] for r in records]
    metas = [r["metadata"] for r in records]

    with dataset_lock:
        ds.tensors["id"].extend(ids)
        ds.tensors["embedding"].extend(embs)
        ds.tensors["text"].extend(texts)
        ds.tensors["metadata"].extend(metas)

def _verify_lengths(ds: deeplake.Dataset) -> None:
    with dataset_lock:
        n = len(ds.tensors["id"])
        for t in ("embedding", "text", "metadata"):
            if len(ds.tensors[t]) != n:
                raise RuntimeError(f"tensor length mismatch: id={n} vs {t}={len(ds.tensors[t])}")

def _safe_commit(ds: deeplake.Dataset, msg: str) -> None:
    _verify_lengths(ds)
    with dataset_lock:
        try:
            ds.commit(msg)
        except Exception:
            pass
        ds.flush()

# ---------------------------------------------------------------------
# Public entrypoint with streaming global batches
# ---------------------------------------------------------------------
def process_repository(
    repo_path: str,
    vec_config: VectorizationConfig,
    dataset_path: str = "deeplake_dataset",
    overwrite_dataset: bool = False,
    *,
    include_git_metadata: bool = False,
    include_git_diffs: bool = False,
    include_full_history: bool = False,  # NOTE: we do NOT duplicate code per commit; this only affects diff behavior if you expand later
    year_filter: Optional[Union[int, Tuple[int, int]]] = None,
    codebase_name: str = "my_codebase",
    max_workers: int = 4,
    only_files: Optional[Iterable[str]] = None,
    include_code: bool = True,
) -> deeplake.Dataset:
    """
    Vectorize files in `repo_path` into a Deep Lake dataset using streaming global batches.

    Key behavior:
    - CODE chunks are stored once (HEAD code), optionally with *latest* commit metadata.
    - DIFF chunks are stored per commit (and each diff gets the correct commit metadata).
    """
    import time as _time
    _t_start = _time.time()

    repo = Repo(repo_path)
    smart_chunker = SmartChunker(vec_config)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(vec_config.model_name, device=device)
    logging.info(f"[model] {vec_config.model_name} on device={device}")

    # Load or create dataset
    try:
        if overwrite_dataset:
            ds = deeplake.empty(dataset_path, overwrite=True)
        else:
            try:
                ds = deeplake.load(dataset_path)
            except DatasetCorruptError:
                logging.warning("Dataset corrupt; resetting to last good snapshot.")
                ds = deeplake.load(dataset_path, reset=True)
    except DatasetHandlerError:
        logging.warning(f"Dataset at '{dataset_path}' not found. Creating new.")
        ds = deeplake.empty(dataset_path, overwrite=True)

    # Ensure tensors exist
    required = {"id": "text", "embedding": "generic", "text": "text", "metadata": "json"}
    for name, htype in required.items():
        if name not in ds.tensors:
            ds.create_tensor(name, htype=htype, chunk_compression="lz4")

    # Build file list
    exts = getattr(vec_config, "file_types_to_vectorize", (".py",))
    if only_files is not None:
        rel_paths = [path_key(p, repo_path) for p in only_files]
        file_list = [rp for rp in rel_paths if any(rp.endswith(ext) for ext in exts)]
    else:
        file_list = list(
            iter_source_files(
                repo_path=repo_path,
                exts=exts,
                include_globs=getattr(vec_config, "include_globs", []),
                exclude_globs=getattr(vec_config, "exclude_globs", []),
                max_file_size_mb=getattr(vec_config, "max_file_size_mb", 2),
                log=logging,
            )
        )

    total_files = len(file_list)
    total_records = 0

    _event({
        "event": "start",
        "codebase": codebase_name,
        "total_files": total_files,
        "dataset_path": dataset_path,
    })

    global_cap = int(getattr(vec_config, "global_batch_cap", 5000))
    micro_bs = int(getattr(vec_config, "batch_size", 64))

    texts_buffer: List[str] = []
    metas_buffer: List[Dict] = []

    def _flush_buffers(batch_idx: int):
        nonlocal total_records
        if not texts_buffer:
            return

        t0 = time.time()
        embs = batched_encode(model, texts_buffer, batch_size=micro_bs)
        t1 = time.time()

        records = [
            {
                "id": str(uuid.uuid4()),
                "embedding": embs[i],
                "text": texts_buffer[i],
                "metadata": metas_buffer[i],
            }
            for i in range(len(texts_buffer))
        ]

        _append_many_fast(ds, records)
        _safe_commit(ds, f"flush-{batch_idx}: {len(records)} records")
        total_records += len(records)

        logging.info(
            f"[flush {batch_idx:03d}] wrote {len(records)} recs | encode={t1-t0:.2f}s total={time.time()-t0:.2f}s (bs={micro_bs})"
        )

        _event({
            "event": "flush",
            "batch": batch_idx,
            "records": len(records),
            "total_records": total_records
        })

        texts_buffer.clear()
        metas_buffer.clear()

    batch_counter = 0
    processed = 0

    with tqdm(
        total=len(file_list),
        desc=f"Processing {codebase_name}",
        unit="file",
        smoothing=0.1,
        dynamic_ncols=True,
        leave=True,
    ) as pbar:

        def _read_and_chunk(rel_path: str):
            abs_path = os.path.join(repo_path, rel_path)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except Exception as e:
                logging.error(f"Error reading {abs_path}: {e}")
                return rel_path, [], []

            # For CODE chunks: use only the latest commit meta (do NOT do full-history duplication)
            latest_meta = get_latest_commit_meta(repo, rel_path, year_filter=year_filter) if include_git_metadata else {}

            code_pairs: List[Tuple[str, Dict]] = []
            if include_code:
                code_chunks = smart_chunker.chunk_file(rel_path, text)
                for ch in code_chunks:
                    base_meta = {
                        "file_path": rel_path,
                        "codebase_name": codebase_name,
                        "chunk_type": ch.chunk_type,
                        "start_line": ch.start_line,
                        "end_line": ch.end_line,
                        "language": ch.language,
                        "source": "code",
                    }
                    combined_meta = {**base_meta, **(latest_meta or {})}
                    code_pairs.append((ch.text, combined_meta))

            # For DIFF chunks: each diff is paired with the correct commit meta (FIXED)
            diff_pairs: List[Tuple[str, Dict]] = []
            if include_git_diffs:
                for diff_text, commit_meta in get_file_diffs_with_meta(repo, rel_path, year_filter=year_filter):
                    diff_chunks = smart_chunker._chunk_by_size(rel_path, diff_text, "git-diff")
                    for dch in diff_chunks:
                        base_meta = {
                            "file_path": rel_path,
                            "codebase_name": codebase_name,
                            "chunk_type": "diff",
                            "start_line": dch.start_line,
                            "end_line": dch.end_line,
                            "language": dch.language,  # 'git-diff'
                            "source": "diff",
                        }
                        combined_meta = {**base_meta, **(commit_meta or {})}
                        diff_pairs.append((dch.text, combined_meta))

            return rel_path, code_pairs, diff_pairs

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_read_and_chunk, rel): rel for rel in file_list}
            for fut in as_completed(futures):
                _rel, code_pairs, diff_pairs = fut.result()

                for txt, meta in code_pairs:
                    texts_buffer.append(txt)
                    metas_buffer.append(meta)
                    if len(texts_buffer) >= global_cap:
                        batch_counter += 1
                        _flush_buffers(batch_counter)

                for txt, meta in diff_pairs:
                    texts_buffer.append(txt)
                    metas_buffer.append(meta)
                    if len(texts_buffer) >= global_cap:
                        batch_counter += 1
                        _flush_buffers(batch_counter)

                processed += 1
                pbar.update(1)

                elapsed = time.time() - _t_start
                eta = None
                if processed > 0:
                    rate = processed / max(elapsed, 1e-6)
                    eta = int((total_files - processed) / max(rate, 1e-6))

                _event({
                    "event": "progress",
                    "processed": processed,
                    "total": total_files,
                    "elapsed_sec": int(elapsed),
                    "eta_sec": eta
                })

    batch_counter += 1
    _flush_buffers(batch_counter)

    _safe_commit(ds, "final-commit")
    elapsed_total = int(time.time() - _t_start)
    logging.info(f"Vectorization complete for '{codebase_name}' → {dataset_path}")

    _event({
        "event": "done",
        "codebase": codebase_name,
        "total_files": total_files,
        "total_records": total_records,
        "dataset_path": dataset_path,
        "elapsed_sec": elapsed_total
    })
    return ds
