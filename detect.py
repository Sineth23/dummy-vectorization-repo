#!/usr/bin/env python3
# update_vector_data.py

import os
import json
import argparse
import deeplake
from deeplake.util.exceptions import DatasetHandlerError
from config import VectorizationConfig
from vectorizestoreFAST import process_repository
from sentence_transformers import SentenceTransformer
from langchain_community.vectorstores import DeepLake as DeepLakeVectorStore


def group_changes_by_repo(changes_list):
    """
    Groups change dictionaries by repository.
    Returns a dict mapping repo paths to lists of change items.
    """
    repo_dict = {}
    for repo_entry in changes_list:
        rp = repo_entry.get("repo_path")
        changes = repo_entry.get("changes", [])
        repo_dict.setdefault(rp, []).extend(changes)
    return repo_dict


def main():
    parser = argparse.ArgumentParser(
        description="Update vector data based on a changes report."
    )
    parser.add_argument("--report_file", type=str, required=True,
                        help="JSON file with detected changes.")
    parser.add_argument("--dataset_path", type=str, required=True,
                        help="Path to the Deep Lake dataset to update or create.")
    args = parser.parse_args()

    report_file = args.report_file
    dataset_path = args.dataset_path

    if not os.path.exists(report_file):
        print(f"[ERROR] No changes report at '{report_file}'. Exiting.")
        return

    with open(report_file, "r", encoding="utf-8") as f:
        changes_data = json.load(f)

    changes_by_repo = group_changes_by_repo(changes_data)

    vec_config = VectorizationConfig(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        max_chunk_size=512
    )

    # Load or create dataset
    print(f"[INFO] Loading Deep Lake dataset at '{dataset_path}'...")
    try:
        ds = deeplake.load(dataset_path)
        print("[INFO] Dataset loaded successfully.")
    except DatasetHandlerError:
        print(f"[WARNING] Dataset not found at '{dataset_path}', creating new.")
        ds = deeplake.empty(dataset_path, overwrite=True)
        print("[INFO] New dataset created.")

    # Wrap in LangChain vector store for deletion
    model = SentenceTransformer(vec_config.model_name)
    db = DeepLakeVectorStore(
        dataset_path=dataset_path,
        embedding=model.encode
    )

    # Process each repository’s changes
    for repo_path, change_items in changes_by_repo.items():
        print(f"\n[INFO] Processing repo: {repo_path}")
        added_or_modified = []
        deleted_files = []
        rename_ops = []
        copy_ops = []
        type_changed = []
        skip_ops = []

        # Classify changes
        for ch in change_items:
            t = ch.get("type")
            if t in ("added", "modified"):
                p = ch.get("file_path")
                if p:
                    added_or_modified.append(p.replace('\\','/'))
            elif t == "deleted":
                p = ch.get("file_path")
                if p:
                    deleted_files.append(p.replace('\\','/'))
            elif t == "rename":
                rename_ops.append(ch)
            elif t == "copy":
                copy_ops.append(ch)
            elif t == "type-changed":
                p = ch.get("file_path")
                if p:
                    type_changed.append(p.replace('\\','/'))
            else:
                skip_ops.append(ch)

        # A) Re-vectorize added or modified
        if added_or_modified:
            print(f"[INFO] Re-vectorizing {len(added_or_modified)} files: {added_or_modified}")
            process_repository(
                repo_path=repo_path,
                vec_config=vec_config,
                dataset_path=dataset_path,
                overwrite_dataset=False,
                include_full_history=False,
                codebase_name=os.path.basename(repo_path),
                only_files=added_or_modified
            )

        # B) Delete embeddings for removed files
        if deleted_files:
            print(f"[INFO] Deleting embeddings for {len(deleted_files)} deleted files: {deleted_files}")
            prefix = os.path.basename(repo_path)
            for df in deleted_files:
                rel = df.replace('\\','/')
                prefixed = f"{prefix}/{rel}"
                for target in (rel, prefixed):
                    print(f"[INFO] Deleting where metadata.file_path == '{target}'")
                    db.delete(filter={"metadata": {"file_path": target}})

        # C) Handle renames (delete old, vectorize new)
        for op in rename_ops:
            old = op.get("old_path","").replace('\\','/')
            new = op.get("new_path","").replace('\\','/')
            print(f"[INFO] Rename: '{old}' -> '{new}'")
            prefix = os.path.basename(repo_path)
            for target in (old, f"{prefix}/{old}"):
                if old:
                    print(f"[INFO] Deleting where metadata.file_path == '{target}'")
                    db.delete(filter={"metadata": {"file_path": target}})
            if new:
                print(f"[INFO] Vectorizing renamed file: {new}")
                process_repository(
                    repo_path=repo_path,
                    vec_config=vec_config,
                    dataset_path=dataset_path,
                    overwrite_dataset=False,
                    include_full_history=False,
                    codebase_name=os.path.basename(repo_path),
                    only_files=[new]
                )

        # D) Handle copies (vectorize new only)
        for op in copy_ops:
            new = op.get("new_path","").replace('\\','/')
            if new:
                print(f"[INFO] Vectorizing copied file: {new}")
                process_repository(
                    repo_path=repo_path,
                    vec_config=vec_config,
                    dataset_path=dataset_path,
                    overwrite_dataset=False,
                    include_full_history=False,
                    codebase_name=os.path.basename(repo_path),
                    only_files=[new]
                )

        # E) Re-vectorize type-changed
        if type_changed:
            print(f"[INFO] Re-vectorizing type-changed files: {type_changed}")
            process_repository(
                repo_path=repo_path,
                vec_config=vec_config,
                dataset_path=dataset_path,
                overwrite_dataset=False,
                include_full_history=False,
                codebase_name=os.path.basename(repo_path),
                only_files=type_changed
            )

        # F) Log unhandled
        if skip_ops:
            print(f"[INFO] Skipping {len(skip_ops)} unhandled items:")
            for s in skip_ops:
                print(f"  - {s}")

    # Final flush
    ds.flush()
    print("[INFO] All changes applied.")


if __name__ == "__main__":
    main()
