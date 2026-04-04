"""
Repository file downloader — fetches all indexable files from GitHub.

Uses existing GitHub API tools with parallel requests and smart filtering.
Skips binaries, vendor dirs, lock files, and oversized files.
"""

import asyncio

import structlog

from src.tools.github.repos import _get_repo_tree, _read_file

log = structlog.get_logger()

# Max concurrent file reads (respect GitHub rate limits)
MAX_CONCURRENT = 10

# Skip files larger than this (almost never useful source code)
MAX_FILE_SIZE = 100_000  # 100KB

# Binary file extensions — never read these
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".webp", ".bmp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib",
    ".pyc", ".pyo", ".class", ".o", ".obj",
    ".mp3", ".mp4", ".wav", ".avi", ".mov",
    ".sqlite", ".db",
}

# Directories to skip entirely
SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".next", ".nuxt",
    "dist", "build", ".cache", ".tox", ".mypy_cache",
    "venv", ".venv", "env", ".env",
    "vendor", "third_party", ".idea", ".vscode",
    "coverage", ".pytest_cache", "htmlcov",
    "eggs", "*.egg-info",
}

# Lock files — skip content but index metadata
LOCK_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "Pipfile.lock", "Cargo.lock",
    "composer.lock", "Gemfile.lock",
}


def _should_skip(path: str, size: int) -> bool:
    """Determine if a file should be skipped."""
    lower = path.lower()
    filename = lower.split("/")[-1]

    # Skip lock files
    if filename in LOCK_FILES:
        return True

    # Skip by extension
    for ext in BINARY_EXTENSIONS:
        if lower.endswith(ext):
            return True

    # Skip by directory
    parts = lower.split("/")
    for part in parts[:-1]:  # check all dirs except filename
        if part in SKIP_DIRS:
            return True
        # Handle wildcard patterns like *.egg-info
        for skip in SKIP_DIRS:
            if "*" in skip and part.endswith(skip.replace("*", "")):
                return True

    # Skip oversized files
    if size > MAX_FILE_SIZE:
        return True

    # Skip minified files
    if ".min." in lower:
        return True

    return False


async def download_repo_files(
    installation_id: int,
    repo_full_name: str,
    ref: str = "HEAD",
) -> dict[str, str]:
    """
    Download all indexable files from a GitHub repo.
    Returns: {file_path: file_content}
    """
    log.info("download_start", repo=repo_full_name)

    # Get full file tree
    tree_result = await _get_repo_tree(installation_id, repo_full_name, ref)
    if not tree_result.success:
        log.error("download_tree_failed", error=tree_result.error)
        return {}

    tree = tree_result.data

    # Filter to indexable files
    indexable = [
        f for f in tree
        if f["type"] == "blob" and not _should_skip(f["path"], f.get("size", 0))
    ]

    log.info(
        "download_filtered",
        repo=repo_full_name,
        total_files=len([f for f in tree if f["type"] == "blob"]),
        indexable_files=len(indexable),
    )

    # Download all files in parallel with semaphore
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    files: dict[str, str] = {}

    async def _read_one(path: str):
        async with semaphore:
            result = await _read_file(installation_id, repo_full_name, path, ref)
            if result.success:
                return path, result.data.get("content", "")
            else:
                log.debug("download_file_failed", path=path, error=result.error)
                return path, None

    tasks = [_read_one(f["path"]) for f in indexable]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            continue
        path, content = result
        if content is not None:
            files[path] = content

    log.info("download_complete", repo=repo_full_name, files_downloaded=len(files))
    return files


async def download_specific_files(
    installation_id: int,
    repo_full_name: str,
    file_paths: list[str],
    ref: str = "HEAD",
) -> dict[str, str]:
    """
    Download specific files (for incremental updates).
    Returns: {file_path: file_content}
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    files: dict[str, str] = {}

    async def _read_one(path: str):
        async with semaphore:
            result = await _read_file(installation_id, repo_full_name, path, ref)
            if result.success:
                return path, result.data.get("content", "")
            return path, None

    tasks = [_read_one(p) for p in file_paths if not _should_skip(p, 0)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            continue
        path, content = result
        if content is not None:
            files[path] = content

    return files
