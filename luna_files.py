"""File helpers for Luna: repo root and write_file returning (ok, path_or_error)."""
import os

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def write_file(relative_path: str, content: str) -> tuple[bool, str]:
    """Write content to path under REPO_ROOT. Returns (True, abs_path) or (False, error_msg)."""
    try:
        path = os.path.join(REPO_ROOT, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True, os.path.abspath(path)
    except Exception as e:
        return False, str(e)
