import os
import uuid
import logging
import numpy as np
from git import Repo
from tqdm import tqdm
from sentence_transformers import SentenceTransformer
from chunking import SmartChunker
from config import VectorizationConfig
import deeplake
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Locks for thread safety if needed
dataset_lock = Lock()
git_lock = Lock()

def remove_file_embeddings(ds, file_path, repo_path):
    """
    Remove all embeddings from the dataset that match 'file_path'.
    'file_path' should be relative to the repo root, as stored in metadata['file_path'].
    """
    to_delete_indices = []
    total_len = len(ds)
    for i in range(total_len):
        item_meta = ds["metadata"][i].data()["value"]  # => dict
        if "file_path" in item_meta:
            # Compare normalized paths
            if item_meta["file_path"].replace("\\","/") == file_path.replace("\\","/"):
                to_delete_indices.append(i)

    if to_delete_indices:
        ds.remove(indices=to_delete_indices)
        logging.info(f"Removed {len(to_delete_indices)} embeddings for file '{file_path}'.")
    else:
        logging.info(f"No embeddings found for file '{file_path}'.")

def process_file(
    file_path,
    repo,
    smart_chunker,
    model,
    ds,
    repo_path,
    codebase_name,
    include_full_history,
    year_filter
):
    """
    Process a single file:
      - Read and chunk it
      - Create embeddings
      - Optionally gather commit history
      - Also embed diffs if needed
    This is your existing logic from 'vectorizestoreFAST.py', adapted for concurrency.
    """
    logging.info(f"Processing file: {file_path}")
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            code = f.read()
    except Exception as e:
        logging.error(f"Error reading {file_path}: {e}")
        return

    # 1) Collect optional metadata from Git, if you want
    #    (Skipping year_filter details for brevity)
    # 2) Chunk the file
    file_chunks = smart_chunker.chunk_file(file_path, code)
    # 3) Embed and store
    for chunk in file_chunks:
        text_to_embed = chunk.text
        embedding = model.encode(text_to_embed)
        with dataset_lock:
            ds.append({
                "id": str(uuid.uuid4()),
                "embedding": embedding.astype(np.float32),
                "text": chunk.text,
                "metadata": {
                    "file_path": chunk.file_path.replace("\\","/"),
                    "chunk_type": chunk.chunk_type,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "language": chunk.language,
                    "codebase_name": codebase_name,
                    # Additional metadata as needed
                }
            })

def process_repository(
    repo_path,
    vec_config,
    dataset_path="deeplake_dataset",
    overwrite_dataset=False,
    include_full_history=False,
    codebase_name="my_codebase",
    max_workers=4,
    batch_size=16,
    year_filter=None,
    only_files=None
):
    """
    Vectorizes a subset (or all) files in 'repo_path' into a Deep Lake dataset.
    'only_files': If provided, must be a list of RELATIVE paths to process.
                  Otherwise, we walk the entire repo looking for known extensions.
    """
    # file extensions to process
    file_extensions = [
        ".py", ".js", ".json", ".ts", ".java", ".cs", ".rb", ".php",
        ".md", ".yaml", ".yml", ".ini", ".go", ".vue"
    ]

    repo = Repo(repo_path)
    smart_chunker = SmartChunker(vec_config)
    model = SentenceTransformer(vec_config.model_name)

    # Create or load dataset
    if overwrite_dataset:
        ds = deeplake.empty(dataset_path, overwrite=True)
    else:
        ds = deeplake.load(dataset_path)

    # Ensure required tensors exist
    for tensor_name, tensor_type in {
        "id": "text",
        "embedding": "generic",
        "text": "text",
        "metadata": "json"
    }.items():
        if tensor_name not in ds.tensors:
            ds.create_tensor(tensor_name, htype=tensor_type, chunk_compression="lz4")

    # Gather target files
    if only_files:
        # Only process these specific relative paths
        all_files = [rel for rel in only_files if any(rel.endswith(ext) for ext in file_extensions)]
    else:
        # Process entire repo for known extensions
        all_files = []
        for root, dirs, files in os.walk(repo_path):
            for file in files:
                if any(file.endswith(ext) for ext in file_extensions):
                    rel_path = os.path.relpath(os.path.join(root, file), repo_path)
                    all_files.append(rel_path)

    # Parallel processing
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        with tqdm(total=len(all_files), desc=f"Processing {codebase_name}") as pbar:
            for rel_path in all_files:
                abs_path = os.path.join(repo_path, rel_path)
                fut = executor.submit(
                    process_file,
                    abs_path,
                    repo,
                    smart_chunker,
                    model,
                    ds,
                    repo_path,
                    codebase_name,
                    include_full_history,
                    year_filter
                )
                futures[fut] = rel_path

            for fut in as_completed(futures):
                fut.result()  # raise exception if any
                pbar.update(1)

    ds.flush()
    logging.info(f"Vectorization complete for repo '{codebase_name}'!")
    return ds
