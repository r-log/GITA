"""End-to-end ingest: walk a local repo, parse source files, and persist
rows into ``repos`` / ``code_index`` / ``import_edges``.

Two modes:
- **Full** (nuke-and-repave): wipe existing rows and re-index everything.
  Used on first index, when ``--full`` is passed, or when the incremental
  path can't determine what changed.
- **Incremental**: detect changed files since ``repos.head_sha`` via
  ``git diff``, delete/re-parse only those files, and re-build their
  import edges. Much faster for repos with 80+ files where only 3 changed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from gita.db.models import CodeIndex, ImportEdge, Repo
from gita.indexer.diff import FileChange, detect_changes, read_head_sha
from gita.indexer.embeddings import EmbeddingClient, prepare_embedding_input
from gita.indexer.imports import discover_package_roots, resolve_import
from gita.indexer.parsers import parse_file
from gita.indexer.walker import LANGUAGE_BY_EXT, _is_skipped, iter_files

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    repo_id: str
    files_indexed: int
    functions_extracted: int
    classes_extracted: int
    edges_total: int
    edges_resolved: int
    head_sha: str | None
    mode: str = "full"  # "full" | "incremental" | "noop"
    files_deleted: int = 0
    files_embedded: int = 0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
async def _get_or_create_repo(
    session: AsyncSession,
    name: str,
    root_path: Path,
    *,
    github_full_name: str | None = None,
) -> Repo:
    stmt = select(Repo).where(Repo.name == name)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.root_path = str(root_path)
        if github_full_name and not existing.github_full_name:
            existing.github_full_name = github_full_name
        return existing
    repo = Repo(
        name=name,
        root_path=str(root_path),
        github_full_name=github_full_name,
    )
    session.add(repo)
    await session.flush()
    return repo


async def _clear_repo_rows(session: AsyncSession, repo_id) -> None:
    await session.execute(
        delete(ImportEdge).where(ImportEdge.repo_id == repo_id)
    )
    await session.execute(
        delete(CodeIndex).where(CodeIndex.repo_id == repo_id)
    )
    await session.flush()


def _parse_and_build_row(
    repo_id,
    root_path: Path,
    relative_path: str,
    language: str,
    head_sha: str | None,
) -> tuple[CodeIndex, int, int] | None:
    """Read, parse, and build a CodeIndex row for one file.

    Returns ``(row, function_count, class_count)`` or ``None`` on failure.
    """
    abs_path = root_path / relative_path
    try:
        content = abs_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        logger.warning(
            "file_read_failed path=%s error=%s", relative_path, exc
        )
        return None

    structure = parse_file(abs_path, content, language)
    structure_json = structure.to_jsonb()

    row = CodeIndex(
        repo_id=repo_id,
        file_path=relative_path,
        language=language,
        content=content,
        line_count=content.count("\n") + (0 if content.endswith("\n") else 1),
        indexed_at_sha=head_sha,
        structure=structure_json,
    )
    return row, len(structure.functions), len(structure.classes)


async def _attach_embeddings(
    rows: list[CodeIndex],
    client: EmbeddingClient | None,
) -> int:
    """Compute and assign ``embedding`` for each row with text content.

    Returns the number of rows that got an embedding. No-ops when
    ``client`` is None or when the row list is empty. Rows with empty
    content are skipped (their embedding stays NULL).

    All rows are embedded in a single ``client.embed`` call so the client
    can batch internally and we pay one round-trip per ingest run.
    """
    if client is None or not rows:
        return 0

    indexed_rows: list[CodeIndex] = []
    inputs: list[str] = []
    for row in rows:
        text = prepare_embedding_input(row.content)
        if not text:
            continue
        indexed_rows.append(row)
        inputs.append(text)

    if not inputs:
        return 0

    vectors = await client.embed(inputs)
    if len(vectors) != len(indexed_rows):
        logger.warning(
            "embedding_count_mismatch expected=%d got=%d",
            len(indexed_rows),
            len(vectors),
        )
        return 0

    for row, vec in zip(indexed_rows, vectors):
        row.embedding = vec
    return len(indexed_rows)


def _build_edges(
    repo_id,
    root_path: Path,
    code_rows: list[CodeIndex],
    package_roots: list[Path],
) -> tuple[list[ImportEdge], int]:
    """Build import edge rows for a list of CodeIndex rows.

    Returns ``(edge_rows, edges_resolved)``.
    """
    edge_rows: list[ImportEdge] = []
    edges_resolved = 0

    for row in code_rows:
        imports = row.structure.get("imports", []) if row.structure else []
        source_file = root_path / row.file_path
        for imp in imports:
            raw = imp.get("raw", "")
            if not raw:
                continue
            resolved = resolve_import(
                raw,
                source_file,
                root_path,
                row.language,
                package_roots=package_roots,
            )
            dst_file: str | None = None
            if resolved is not None:
                try:
                    dst_file = str(
                        resolved.resolve().relative_to(root_path)
                    ).replace("\\", "/")
                    edges_resolved += 1
                except ValueError:
                    dst_file = None
            edge_rows.append(
                ImportEdge(
                    repo_id=repo_id,
                    src_file=row.file_path,
                    dst_file=dst_file,
                    raw_import=raw,
                    language=row.language,
                )
            )

    return edge_rows, edges_resolved


# ---------------------------------------------------------------------------
# Full index (nuke-and-repave)
# ---------------------------------------------------------------------------
async def _full_index(
    session: AsyncSession,
    repo: Repo,
    root_path: Path,
    head_sha: str | None,
    package_roots: list[Path],
    include_tests: bool,
    embedding_client: EmbeddingClient | None,
) -> IngestResult:
    await _clear_repo_rows(session, repo.id)

    code_rows: list[CodeIndex] = []
    functions_total = 0
    classes_total = 0

    for discovered in iter_files(root_path, include_tests=include_tests):
        result = _parse_and_build_row(
            repo.id,
            root_path,
            discovered.relative_path,
            discovered.language,
            head_sha,
        )
        if result is None:
            continue
        row, fn_count, cls_count = result
        code_rows.append(row)
        functions_total += fn_count
        classes_total += cls_count

    session.add_all(code_rows)
    await session.flush()

    files_embedded = await _attach_embeddings(code_rows, embedding_client)

    edge_rows, edges_resolved = _build_edges(
        repo.id, root_path, code_rows, package_roots
    )
    session.add_all(edge_rows)

    repo.head_sha = head_sha
    repo.indexed_at = datetime.now(UTC)
    await session.flush()

    return IngestResult(
        repo_id=str(repo.id),
        files_indexed=len(code_rows),
        functions_extracted=functions_total,
        classes_extracted=classes_total,
        edges_total=len(edge_rows),
        edges_resolved=edges_resolved,
        head_sha=head_sha,
        mode="full",
        files_embedded=files_embedded,
    )


# ---------------------------------------------------------------------------
# Incremental index (selective update)
# ---------------------------------------------------------------------------
def _is_indexable(relative_path: str, include_tests: bool) -> str | None:
    """Return the language if the file passes the walker's filter, else None."""
    p = Path(relative_path)
    language = LANGUAGE_BY_EXT.get(p.suffix)
    if language is None:
        return None
    if _is_skipped(p, include_tests):
        return None
    return language


async def _incremental_index(
    session: AsyncSession,
    repo: Repo,
    root_path: Path,
    head_sha: str | None,
    package_roots: list[Path],
    changes: list[FileChange],
    include_tests: bool,
    embedding_client: EmbeddingClient | None,
) -> IngestResult:
    files_deleted = 0
    code_rows: list[CodeIndex] = []
    functions_total = 0
    classes_total = 0

    for change in changes:
        if change.status == "deleted":
            # Remove the file's code_index row + its outgoing import edges.
            await session.execute(
                delete(ImportEdge)
                .where(ImportEdge.repo_id == repo.id)
                .where(ImportEdge.src_file == change.relative_path)
            )
            await session.execute(
                delete(CodeIndex)
                .where(CodeIndex.repo_id == repo.id)
                .where(CodeIndex.file_path == change.relative_path)
            )
            files_deleted += 1
            continue

        # added or modified — check if it's an indexable source file.
        language = _is_indexable(change.relative_path, include_tests)
        if language is None:
            continue

        if change.status == "modified":
            # Remove old row + old outgoing edges before re-inserting.
            await session.execute(
                delete(ImportEdge)
                .where(ImportEdge.repo_id == repo.id)
                .where(ImportEdge.src_file == change.relative_path)
            )
            await session.execute(
                delete(CodeIndex)
                .where(CodeIndex.repo_id == repo.id)
                .where(CodeIndex.file_path == change.relative_path)
            )

        result = _parse_and_build_row(
            repo.id,
            root_path,
            change.relative_path,
            language,
            head_sha,
        )
        if result is None:
            continue
        row, fn_count, cls_count = result
        code_rows.append(row)
        functions_total += fn_count
        classes_total += cls_count

    await session.flush()
    session.add_all(code_rows)
    await session.flush()

    files_embedded = await _attach_embeddings(code_rows, embedding_client)

    # Rebuild import edges for the newly parsed files.
    edge_rows, edges_resolved = _build_edges(
        repo.id, root_path, code_rows, package_roots
    )
    session.add_all(edge_rows)

    repo.head_sha = head_sha
    repo.indexed_at = datetime.now(UTC)
    await session.flush()

    logger.info(
        "incremental_index repo=%s added_or_modified=%d deleted=%d edges=%d embedded=%d",
        repo.name,
        len(code_rows),
        files_deleted,
        len(edge_rows),
        files_embedded,
    )

    return IngestResult(
        repo_id=str(repo.id),
        files_indexed=len(code_rows),
        functions_extracted=functions_total,
        classes_extracted=classes_total,
        edges_total=len(edge_rows),
        edges_resolved=edges_resolved,
        head_sha=head_sha,
        mode="incremental",
        files_deleted=files_deleted,
        files_embedded=files_embedded,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def index_repository(
    session: AsyncSession,
    repo_name: str,
    root_path: Path,
    *,
    include_tests: bool = False,
    force_full: bool = False,
    github_full_name: str | None = None,
    embedding_client: EmbeddingClient | None = None,
) -> IngestResult:
    """Ingest a local repo into the three tables.

    Tries incremental update when possible. Falls back to full re-index
    when ``force_full=True``, the repo has no ``head_sha`` (first index),
    or ``git diff`` fails.

    ``github_full_name`` (e.g. ``"r-log/AMASS"``) is stored on the Repo
    row so webhook-triggered jobs can resolve the repo by its GitHub name.

    ``embedding_client`` is optional: when provided, each indexed file's
    content is embedded and stored in ``code_index.embedding``. When
    ``None``, the embedding column stays NULL and concept_view will fall
    back to keyword-only FTS. Incremental updates only embed the files
    they re-parse — unchanged files keep whatever embedding they had.

    Caller owns the transaction — this function does NOT commit.
    """
    root_path = root_path.resolve()
    if not root_path.is_dir():
        raise ValueError(f"root_path is not a directory: {root_path}")

    repo = await _get_or_create_repo(
        session, repo_name, root_path, github_full_name=github_full_name
    )
    head_sha = read_head_sha(root_path)
    package_roots = discover_package_roots(root_path)

    # Decide: full or incremental?
    use_full = force_full or repo.head_sha is None

    if not use_full and head_sha is not None:
        if repo.head_sha == head_sha:
            # Nothing changed since last index.
            logger.info(
                "index_noop repo=%s head_sha=%s", repo_name, head_sha
            )
            repo.indexed_at = datetime.now(UTC)
            await session.flush()
            return IngestResult(
                repo_id=str(repo.id),
                files_indexed=0,
                functions_extracted=0,
                classes_extracted=0,
                edges_total=0,
                edges_resolved=0,
                head_sha=head_sha,
                mode="noop",
            )

        changes = detect_changes(root_path, repo.head_sha)
        if changes is not None:
            return await _incremental_index(
                session,
                repo,
                root_path,
                head_sha,
                package_roots,
                changes,
                include_tests,
                embedding_client,
            )
        # detect_changes returned None → fallback to full.
        logger.warning(
            "incremental_fallback_to_full repo=%s reason=detect_changes_failed",
            repo_name,
        )

    return await _full_index(
        session,
        repo,
        root_path,
        head_sha,
        package_roots,
        include_tests,
        embedding_client,
    )
