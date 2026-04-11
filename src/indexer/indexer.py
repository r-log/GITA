"""
Code Indexer orchestrator — ties together download, parse, store, and code map generation.

Two entry points:
- index_repository(): Full index on first install
- reindex_files(): Incremental update on push (changed files only)
"""

from datetime import datetime

import structlog
from sqlalchemy import select, delete

from src.core.database import async_session
from src.models.code_index import CodeIndex
from src.indexer.downloader import download_repo_files, download_specific_files
from src.indexer.parsers import parse_file
from src.indexer.code_map import generate_code_map
from src.indexer.graph_builder import build_graph_for_repo, update_graph_for_files

log = structlog.get_logger()


# Languages whose raw content we keep in the DB for granular retrieval tools.
# Test/generated/vendored paths are excluded by _should_store_content().
_STORABLE_LANGUAGES = {
    "python", "typescript", "javascript",
    "go", "rust", "java", "ruby", "php", "c_sharp", "kotlin",
    "vue", "svelte",
    "json", "yaml", "toml",  # config-as-logic files the LLM may want to read
}

# Max stored content size per file. Files larger than this get content=None
# (structure is still extracted). 200KB covers ~99% of real source files.
_MAX_CONTENT_BYTES = 200_000

_EXCLUDE_PATH_SEGMENTS = (
    "/tests/", "/test/", "/__tests__/", "/spec/", "/specs/",
    "/__pycache__/", "/node_modules/", "/vendor/", "/dist/", "/build/",
    "/.venv/", "/venv/", "/.git/", "/target/",
)

_EXCLUDE_SUFFIXES = (
    ".min.js", ".min.css", "_pb2.py", ".lock",
)


def _should_store_content(file_path: str, language: str, content: str) -> bool:
    """Decide whether to persist raw content for this file in code_index.content.

    Parses always run — this only gates whether the LLM can pull slices via
    code_retrieval. Tests, generated code, vendored deps, and oversized files
    get structure-only; everything else in an allowlisted source language is
    stored in full.
    """
    if language not in _STORABLE_LANGUAGES:
        return False
    if len(content.encode("utf-8", errors="replace")) > _MAX_CONTENT_BYTES:
        return False
    path_lower = "/" + file_path.lower()  # leading slash so segment matches work at root
    for segment in _EXCLUDE_PATH_SEGMENTS:
        if segment in path_lower:
            return False
    for suffix in _EXCLUDE_SUFFIXES:
        if path_lower.endswith(suffix):
            return False
    # Filename-based exclusions (test_*.py, *_test.go, *.spec.ts, etc.)
    filename = file_path.rsplit("/", 1)[-1].lower()
    if filename.startswith(("test_", "tests_")):
        return False
    if filename.endswith(("_test.go", "_test.py", ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
                          ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")):
        return False
    return True


async def index_repository(
    installation_id: int,
    repo_full_name: str,
    repo_id: int,
) -> str:
    """
    Full repository index: download all files, parse, store in DB, generate code map.

    Returns the code map text.
    """
    log.info("index_start", repo=repo_full_name)

    # 1. Download all indexable files
    files = await download_repo_files(installation_id, repo_full_name)
    if not files:
        log.warning("index_no_files", repo=repo_full_name)
        return "# Empty Repository\nNo indexable source files found."

    # 2. Parse all files (keep raw content alongside for storable files)
    parsed: list = []
    raw_contents: dict[str, str] = {}  # file_path -> raw source, for DB persistence
    for path, content in files.items():
        file_index = parse_file(content, path)
        parsed.append(file_index)
        if _should_store_content(path, file_index.language, content):
            raw_contents[path] = content

    log.info(
        "index_parsed",
        repo=repo_full_name,
        files=len(parsed),
        content_stored=len(raw_contents),
    )

    # 3. Store in DB (upsert: delete old + insert new)
    async with async_session() as session:
        # Delete existing index for this repo
        await session.execute(
            delete(CodeIndex).where(CodeIndex.repo_id == repo_id)
        )

        # Insert all new records
        for fi in parsed:
            record = CodeIndex(
                repo_id=repo_id,
                file_path=fi.file_path,
                language=fi.language,
                size_bytes=fi.size_bytes,
                line_count=fi.line_count,
                structure=fi.structure,
                content=raw_contents.get(fi.file_path),
                content_hash=fi.content_hash,
            )
            session.add(record)

        await session.commit()

    log.info("index_stored", repo=repo_full_name, records=len(parsed))

    # 4. Build code knowledge graph
    await build_graph_for_repo(repo_id, parsed)

    # 5. Generate code map
    records_for_map = [
        {
            "file_path": fi.file_path,
            "language": fi.language,
            "line_count": fi.line_count,
            "structure": fi.structure,
        }
        for fi in parsed
    ]
    code_map = generate_code_map(records_for_map, project_name=repo_full_name)

    log.info("index_complete", repo=repo_full_name, code_map_size=len(code_map))
    return code_map


async def reindex_files(
    installation_id: int,
    repo_full_name: str,
    repo_id: int,
    changed_files: set[str],
    removed_files: set[str],
) -> dict:
    """
    Incremental reindex: re-parse only changed files, remove deleted ones.
    Zero LLM cost — entirely deterministic.

    Returns summary dict.
    """
    log.info(
        "reindex_start",
        repo=repo_full_name,
        changed=len(changed_files),
        removed=len(removed_files),
    )

    # 1. Download changed files
    files_to_download = list(changed_files)
    downloaded = {}
    if files_to_download:
        downloaded = await download_specific_files(
            installation_id, repo_full_name, files_to_download
        )

    # 2. Parse downloaded files (keep raw content alongside for storable files)
    parsed: list = []
    raw_contents: dict[str, str] = {}
    for path, content in downloaded.items():
        file_index = parse_file(content, path)
        parsed.append(file_index)
        if _should_store_content(path, file_index.language, content):
            raw_contents[path] = content

    # 3. Update DB
    async with async_session() as session:
        # Remove deleted files
        if removed_files:
            for path in removed_files:
                await session.execute(
                    delete(CodeIndex).where(
                        CodeIndex.repo_id == repo_id,
                        CodeIndex.file_path == path,
                    )
                )

        # Upsert changed files
        for fi in parsed:
            # Check if record exists
            existing = await session.execute(
                select(CodeIndex).where(
                    CodeIndex.repo_id == repo_id,
                    CodeIndex.file_path == fi.file_path,
                )
            )
            record = existing.scalar_one_or_none()

            if record:
                # Update existing
                record.language = fi.language
                record.size_bytes = fi.size_bytes
                record.line_count = fi.line_count
                record.structure = fi.structure
                record.content = raw_contents.get(fi.file_path)
                record.content_hash = fi.content_hash
                record.updated_at = datetime.utcnow()
            else:
                # Insert new
                session.add(CodeIndex(
                    repo_id=repo_id,
                    file_path=fi.file_path,
                    language=fi.language,
                    size_bytes=fi.size_bytes,
                    line_count=fi.line_count,
                    structure=fi.structure,
                    content=raw_contents.get(fi.file_path),
                    content_hash=fi.content_hash,
                ))

        await session.commit()

    # 4. Update code knowledge graph
    await update_graph_for_files(repo_id, parsed, removed_files)

    log.info(
        "reindex_complete",
        repo=repo_full_name,
        updated=len(parsed),
        removed=len(removed_files),
    )

    return {
        "files_updated": len(parsed),
        "files_removed": len(removed_files),
    }


async def get_code_map_for_repo(repo_id: int, project_name: str = "") -> str:
    """Load code index from DB and generate code map on the fly."""
    async with async_session() as session:
        result = await session.execute(
            select(CodeIndex).where(CodeIndex.repo_id == repo_id)
        )
        records = result.scalars().all()

    if not records:
        return "# No Index\nRepository has not been indexed yet."

    records_for_map = [
        {
            "file_path": r.file_path,
            "language": r.language,
            "line_count": r.line_count,
            "structure": r.structure,
        }
        for r in records
    ]

    return generate_code_map(records_for_map, project_name=project_name)
