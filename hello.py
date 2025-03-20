print("hello")
#!/usr/bin/env python3
import os
import json
import deeplake
from config import VectorizationConfig
from vectorizestoreFAST import process_repository, remove_file_embeddings

def group_changes_by_repo(changes_list):
    """
    Takes a list of changes dicts like:
    [
      {
        "repo_path": "C:\\Users\\Sineth\\eventpay-backend",
        "timestamp": "...",
        "changes": [
          {"type": "modified", "file_path": "src/app.py"},
          {"type": "deleted", "file_path": "docs/old_config.md"},
          {"type": "rename", "old_path": "src/foo.py", "new_path": "src/bar.py", "rename_score": "100"}
        ]
      }, ...
    ]
    Returns a dict:
    {
      "C:\\Users\\Sineth\\eventpay-backend": [ {...}, {...} ],
      "C:\\Users\\Sineth\\eventpay-react-consumer": [ {...} ],
      ...
    }
    """
    repo_dict = {}
    for repo_entry in changes_list:
        rp = repo_entry["repo_path"]
        if rp not in repo_dict:
            repo_dict[rp] = []
        for change_item in repo_entry["changes"]:
            repo_dict[rp].append(change_item)
    return repo_dict

def main():
    # 1. Load the monthly changes JSON
    changes_report_file = "changes_report_2025_03.json"  # or dynamically detect
    if not os.path.exists(changes_report_file):
        print(f"No monthly report found: {changes_report_file}")
        return

    with open(changes_report_file, "r", encoding="utf-8") as f:
        changes_data = json.load(f)

    # 2. Group changes by repo
    changes_by_repo = group_changes_by_repo(changes_data)

    # 3. Load vector config & Deep Lake dataset
    vec_config = VectorizationConfig(
        model_name="sentence-transformers/all-MiniLM-L6-v2",
        max_chunk_size=512,
    )
    dataset_path = "ticketlabs_project"  # your chosen dataset path
    ds = deeplake.load(dataset_path)

    # 4. For each repo, handle all changes
    for repo_path, change_items in changes_by_repo.items():
        print(f"\n=== Updating vector data for repo: {repo_path} ===")

        # We'll track sets of paths to process in one pass:
        added_or_modified = []
        to_remove = []

        # We'll handle rename and copy distinctly
        rename_operations = []
        copy_operations = []
        skip_operations = []  # type-changed, unmerged, unknown, etc.

        for ch in change_items:
            ch_type = ch.get("type", "")
            # 1) Basic statuses
            if ch_type in ["added", "modified"]:
                path = ch.get("file_path")
                if path:
                    added_or_modified.append(path)

            elif ch_type == "deleted":
                path = ch.get("file_path")
                if path:
                    to_remove.append(path)

            # 2) Renamed
            elif ch_type == "rename":
                # We might remove old_path's embeddings and treat new_path as added
                rename_operations.append(ch)

            # 3) Copied
            elif ch_type == "copy":
                copy_operations.append(ch)

            # 4) Type-changed
            elif ch_type == "type-changed":
                # Often treat as 'modified' if content changed. Might need a custom check.
                path = ch.get("file_path")
                if path:
                    added_or_modified.append(path)

            # 5) Unmerged, broken, unknown => skip or log
            elif ch_type in ["unmerged", "broken", "unknown", "other"]:
                skip_operations.append(ch)
            else:
                skip_operations.append(ch)

        # Step A: Handle added_or_modified in one pass
        if added_or_modified:
            print(f"Re-vectorizing {len(added_or_modified)} files (added/modified).")
            codebase_name = os.path.basename(repo_path)
            process_repository(
                repo_path=repo_path,
                vec_config=vec_config,
                dataset_path=dataset_path,
                overwrite_dataset=False,  # never overwrite the entire dataset
                include_full_history=False,  # or True, up to you
                codebase_name=codebase_name,
                only_files=added_or_modified
            )

        # Step B: Handle deleted files
        if to_remove:
            print(f"Removing embeddings for {len(to_remove)} deleted files.")
            for path in to_remove:
                remove_file_embeddings(ds, path, repo_path)

        # Step C: Handle rename
        # Typically remove old path embeddings, then add new path
      

            # 1) Remove old
            if old_path:
                remove_file_embeddings(ds, old_path, repo_path)

            # 2) Vectorize new
            if new_path:
                codebase_name = os.path.basename(repo_path)
                process_repository(
                    repo_path=repo_path,
                    vec_config=vec_config,
                    dataset_path=dataset_path,
                    overwrite_dataset=False,
                    include_full_history=False,
                    codebase_name=codebase_name,
                    only_files=[new_path]
                )

        # Step D: Handle copy
        # Usually treat as "added" for new_path
        for c_op in copy_operations:
            old_path = c_op.get("old_path")
            new_path = c_op.get("new_path")
            copy_score = c_op.get("copy_score", "")
            print(f"Copy: old='{old_path}' -> new='{new_path}' (score={copy_score})")
            # If you want to skip duplicates if content is identical, you can do so
            # For now, treat new_path as an added file.
            if new_path:
                codebase_name = os.path.basename(repo_path)
                process_repository(
                    repo_path=repo_path,
                    vec_config=vec_config,
                    dataset_path=dataset_path,
                    overwrite_dataset=False,
                    include_full_history=False,
                    codebase_name=codebase_name,
                    only_files=[new_path]
                )

        # Step E: Skip or log unmerged, broken, unknown, etc.
        if skip_operations:
            print(f"Skipping {len(skip_operations)} items with special statuses (unmerged/broken/unknown/...)")

    ds.flush()
    print("\nAll changes applied to the dataset!")

if __name__ == "__main__":
    main()
