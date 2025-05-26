
import os
import json
import datetime
import argparse
import subprocess
from git import Repo, exc
from typing import List, Dict

def load_last_commits(path: str) -> Dict[str, str]:
    """Load the JSON mapping of repo_path → last_seen_commit."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

