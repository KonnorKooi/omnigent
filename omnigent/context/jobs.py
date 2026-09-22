"""The Update-context job and proposal storage (``designs/PROJECT_CONTEXT.md`` §4.5).

One in-process asyncio job per project at a time (single-user local scope);
status is polled through the jobs route. Steps are logged and individually
skippable:

1. **code_graph** — ``graphify extract <repo> --code-only`` into a temporary
   directory, ``graphify cluster-only --no-label --no-viz``, then copy the
   outputs into ``<context>/graph/``.
2. **proposals** — gather ``raw/`` files and project session transcripts
   changed since ``.last_update``, run the curator, and store each valid
   per-file draft as ``.proposals/<id>.json``.
3. **stamp** — write ``.last_update`` (only once the proposals step has
   consumed the inputs, so a failed or skipped curator pass is retried).

Proposals are applied through :meth:`ContextService.write` with the sha the
file had when the proposal was made, so a file edited in the meantime yields a
409 instead of being overwritten.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import math
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from omnigent.context.curator import ContextCurator, CuratorInput, CuratorRequest
from omnigent.context.service import (
    MAX_SCAN_BYTES,
    MAX_WRITE_BYTES,
    PROPOSALS_DIR,
    ContextService,
    is_secret_name,
)
from omnigent.errors import ErrorCode, OmnigentError

_logger = logging.getLogger(__name__)

#: Timeout for each graphify subprocess.
GRAPHIFY_TIMEOUT_S = 900.0
#: Files copied out of ``graphify-out/`` into ``graph/``.
GRAPHIFY_OUTPUT_FILES = ("graph.json", "GRAPH_REPORT.md", "manifest.json")
#: Maximum raw inputs handed to the curator per run.
MAX_RAW_INPUTS = 50
#: Finished jobs remembered per project for polling.
_MAX_FINISHED_JOBS = 20

#: Gathers session transcripts updated since a timestamp (``None`` = ever).
SessionGatherer = Callable[[int | None], Awaitable[list[CuratorInput]]]
#: Returns a curator, or ``None`` when none is available on this machine.
CuratorFactory = Callable[[], ContextCurator | None]


@dataclasses.dataclass
class JobStep:
    """One step's progress.

    :param name: ``"code_graph"``, ``"proposals"`` or ``"stamp"``.
    :param status: ``pending``/``running``/``succeeded``/``skipped``/``failed``.
    :param detail: Short human explanation.
    """

    name: str
    status: str = "pending"
    detail: str | None = None


@dataclasses.dataclass
class ContextJob:
    """State of one Update-context run.

    :param id: Job id.
    :param project_id: Owning project.
    :param status: ``running``, ``succeeded`` or ``failed``.
    :param started_at: Unix start time.
    :param finished_at: Unix end time, once done.
    :param steps: Per-step progress.
    :param log: Human-readable log lines.
    :param proposals_created: Proposals written by this run.
    :param error: Failure summary when ``status == "failed"``.
    """

    id: str
    project_id: str
    status: str
    started_at: int
    steps: list[JobStep]
    finished_at: int | None = None
    log: list[str] = dataclasses.field(default_factory=list)
    proposals_created: int = 0
    error: str | None = None
    inputs_gathered_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the API.

        :returns: A JSON-safe dict.
        """
        return dataclasses.asdict(self)

    def step(self, name: str) -> JobStep:
        """Look up a step by name.

        :param name: Step name.
        :returns: The step.
        """
        return next(s for s in self.steps if s.name == name)

    def note(self, message: str) -> None:
        """Append a log line (also mirrored to the server log).

        :param message: Line to record.
        """
        self.log.append(message)
        _logger.info("context update %s: %s", self.id, message)


# ── Proposals ────────────────────────────────────────────────────────────


class ProposalStore:
    """Pending proposals stored as JSON files in ``<context>/.proposals/``.

    :param service: The project's context service.
    """

    def __init__(self, service: ContextService) -> None:
        self.service = service
        self.dir = service.root / PROPOSALS_DIR

    def _path(self, proposal_id: str) -> Path:
        """Location of one proposal, validating the id shape.

        :param proposal_id: Hex id.
        :returns: The JSON file path.
        :raises OmnigentError: 404 for ids that are not plain hex.
        """
        if not proposal_id or not all(c in "0123456789abcdef" for c in proposal_id):
            raise OmnigentError("Proposal not found", code=ErrorCode.NOT_FOUND)
        return self.dir / f"{proposal_id}.json"

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        """Persist a new proposal record.

        :param record: Fields other than ``id``/``created_at``.
        :returns: The stored record.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        stored = {"id": uuid.uuid4().hex, "created_at": int(time.time()), **record}
        self._path(stored["id"]).write_text(json.dumps(stored, indent=2), encoding="utf-8")
        return stored

    def get(self, proposal_id: str) -> dict[str, Any]:
        """Load one proposal.

        :param proposal_id: Proposal id.
        :returns: The stored record.
        :raises OmnigentError: 404 when missing or unreadable.
        """
        path = self._path(proposal_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OmnigentError("Proposal not found", code=ErrorCode.NOT_FOUND) from exc
        if not isinstance(record, dict):
            raise OmnigentError("Proposal not found", code=ErrorCode.NOT_FOUND)
        return record

    def delete(self, proposal_id: str) -> None:
        """Remove a proposal.

        :param proposal_id: Proposal id.
        :raises OmnigentError: 404 when missing.
        """
        path = self._path(proposal_id)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise OmnigentError("Proposal not found", code=ErrorCode.NOT_FOUND) from exc

    def list(self) -> list[dict[str, Any]]:
        """All pending proposals, oldest first, with current file state.

        :returns: Records extended with ``current_content`` and ``stale``.
        """
        if not self.dir.is_dir():
            return []
        out: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                record = self.get(path.stem)
            except OmnigentError:
                continue
            out.append(self._with_current(record))
        out.sort(key=lambda r: (r.get("created_at", 0), r["id"]))
        return out

    def count(self) -> int:
        """Number of pending proposals.

        :returns: Count of stored proposal files.
        """
        return len(list(self.dir.glob("*.json"))) if self.dir.is_dir() else 0

    def _with_current(self, record: dict[str, Any]) -> dict[str, Any]:
        """Attach the file's present content and a staleness flag.

        :param record: Stored proposal.
        :returns: A copy with ``current_content`` and ``stale``.
        """
        current_content: str | None = None
        current_sha: str | None = None
        try:
            read = self.service.read(record["path"])
            current_content, current_sha = read["content"], read["sha"]
        except OmnigentError:
            pass
        return {
            **record,
            "current_content": current_content,
            "stale": current_sha != record.get("base_sha"),
        }

    def apply(self, proposal_id: str) -> dict[str, Any]:
        """Write a proposal's content and remove it.

        :param proposal_id: Proposal id.
        :returns: The write result (``path``, ``sha``, ``commit``, ...).
        :raises OmnigentError: 409 when the file changed since the proposal.
        """
        record = self.get(proposal_id)
        rationale = " ".join(str(record.get("rationale") or "").split())
        if len(rationale) > 72:
            rationale = rationale[:69] + "..."
        message = f"context: apply proposal {proposal_id}" + (
            f" — {rationale}" if rationale else ""
        )
        result = self.service.write(
            record["path"],
            record["new_content"],
            record.get("base_sha"),
            commit_message=message,
        )
        self.delete(proposal_id)
        return result


# ── Steps ────────────────────────────────────────────────────────────────


def run_graphify_code_graph(repo_path: Path, graph_dir: Path, log: Callable[[str], None]) -> int:
    """Refresh ``graph_dir`` from a code-only graphify extraction of ``repo_path``.

    :param repo_path: Source repository to index (read-only; outputs go to a
        temporary directory, never into the repo).
    :param graph_dir: The context repository's ``graph/`` directory.
    :param log: Sink for progress lines.
    :returns: Number of files copied.
    :raises RuntimeError: When graphify fails or times out.
    """
    graphify = shutil.which("graphify")
    if graphify is None:
        raise RuntimeError("graphify is not on PATH")
    with tempfile.TemporaryDirectory(prefix="omnigent-graphify-") as tmp:
        commands = [
            [graphify, "extract", str(repo_path), "--code-only", "--no-cluster", "--out", tmp],
            [graphify, "cluster-only", tmp, "--no-label", "--no-viz"],
        ]
        for argv in commands:
            log(f"$ graphify {' '.join(argv[1:3])} ...")
            try:
                # argv list, never a shell; bounded by a timeout.
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=GRAPHIFY_TIMEOUT_S,
                    check=False,
                    cwd=tmp,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"graphify timed out after {GRAPHIFY_TIMEOUT_S:.0f}s") from exc
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
            for line in tail:
                log(f"  {line}")
            if proc.returncode != 0:
                raise RuntimeError(f"graphify exited {proc.returncode}")
        out_dir = Path(tmp) / "graphify-out"
        if not (out_dir / "graph.json").is_file():
            raise RuntimeError("graphify produced no graph.json")
        graph_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for name in GRAPHIFY_OUTPUT_FILES:
            source = out_dir / name
            if source.is_file():
                shutil.copyfile(source, graph_dir / name)
                copied += 1
        return copied


def gather_raw_inputs(service: ContextService, since: float | None) -> list[CuratorInput]:
    """New or changed text files under ``raw/``.

    :param service: The context service.
    :param since: Only files modified after this unix time (``None`` = all).
    :returns: Up to :data:`MAX_RAW_INPUTS` inputs, newest first.
    """
    candidates: list[tuple[float, str, Path]] = []
    for rel, abs_path in service.iter_files():
        if not rel.startswith("raw/"):
            continue
        name = rel.rsplit("/", 1)[-1]
        if is_secret_name(name) or not name.lower().endswith((".md", ".txt")):
            continue
        try:
            stat = abs_path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_SCAN_BYTES or (since is not None and stat.st_mtime <= since):
            continue
        candidates.append((stat.st_mtime, rel, abs_path))
    candidates.sort(key=lambda c: (-c[0], c[1]))
    inputs: list[CuratorInput] = []
    for _, rel, abs_path in candidates[:MAX_RAW_INPUTS]:
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        inputs.append(CuratorInput(kind="raw", ref=rel, title=rel, text=text))
    return inputs


def _current_files(service: ContextService) -> dict[str, str]:
    """Current ``system/``/``wiki/`` markdown for the curator prompt.

    :param service: The context service.
    :returns: path → content.
    """
    files: dict[str, str] = {}
    for rel, abs_path in service.iter_files():
        if rel.startswith(("system/", "wiki/")) and rel.lower().endswith(".md"):
            try:
                files[rel] = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return files


def validate_draft(service: ContextService, path: str, action: str, content: str) -> str | None:
    """Check a curator draft against the write rules.

    :param service: The context service.
    :param path: Proposed path.
    :param action: ``create`` or ``edit``.
    :param content: Proposed content.
    :returns: A rejection reason, or ``None`` when acceptable.
    """
    try:
        resolved = service.resolve(path)
    except OmnigentError as exc:
        return exc.message
    if not service.is_writable_rel(resolved.rel):
        return "not a writable system/ or wiki/ markdown path"
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return "content exceeds the size limit"
    exists = resolved.abs_path.is_file()
    if action == "create" and exists:
        return "create targets an existing file"
    if action == "edit" and not exists:
        return "edit targets a missing file"
    if exists and service.read(resolved.rel).get("content") == content:
        return "no change"
    return None


# ── Manager ──────────────────────────────────────────────────────────────


class ContextJobManager:
    """Runs and tracks Update-context jobs (one at a time per project).

    :param curator_factory: Supplies the curator for the proposals step.
    :param code_graph_runner: Replaceable graphify step (tests inject a fake).
    """

    def __init__(
        self,
        curator_factory: CuratorFactory,
        *,
        code_graph_runner: Callable[[Path, Path, Callable[[str], None]], int] = (
            run_graphify_code_graph
        ),
    ) -> None:
        self._curator_factory = curator_factory
        self._code_graph_runner = code_graph_runner
        self._jobs: dict[str, list[ContextJob]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def active(self, project_id: str) -> ContextJob | None:
        """The running job for a project, if any.

        :param project_id: Project id.
        :returns: The running job or ``None``.
        """
        jobs = self._jobs.get(project_id, [])
        return next((j for j in reversed(jobs) if j.status == "running"), None)

    def get(self, project_id: str, job_id: str) -> ContextJob:
        """Look up a job.

        :param project_id: Project id (jobs are scoped per project).
        :param job_id: Job id.
        :returns: The job.
        :raises OmnigentError: 404 when unknown.
        """
        for job in self._jobs.get(project_id, []):
            if job.id == job_id:
                return job
        raise OmnigentError("Job not found", code=ErrorCode.NOT_FOUND)

    def start(
        self,
        project_id: str,
        service: ContextService,
        gather_sessions: SessionGatherer,
        *,
        code_graph: bool = True,
        proposals: bool = True,
    ) -> ContextJob:
        """Start a job in the background.

        :param project_id: Project id.
        :param service: The project's context service.
        :param gather_sessions: Transcript gatherer for the proposals step.
        :param code_graph: Run the code graph step.
        :param proposals: Run the proposals step.
        :returns: The new (running) job.
        :raises OmnigentError: 409 when a job is already running.
        """
        running = self.active(project_id)
        if running is not None:
            raise OmnigentError(
                f"An update is already running (job {running.id})", code=ErrorCode.CONFLICT
            )
        if not service.initialized():
            raise OmnigentError(
                "Initialize the context folder before updating it", code=ErrorCode.INVALID_INPUT
            )
        job = ContextJob(
            id=uuid.uuid4().hex,
            project_id=project_id,
            status="running",
            started_at=int(time.time()),
            steps=[JobStep("code_graph"), JobStep("proposals"), JobStep("stamp")],
        )
        history = self._jobs.setdefault(project_id, [])
        history.append(job)
        del history[:-_MAX_FINISHED_JOBS]
        self._tasks[job.id] = asyncio.create_task(
            self._run(job, service, gather_sessions, code_graph=code_graph, proposals=proposals)
        )
        return job

    async def wait(self, job_id: str) -> None:
        """Await a job's background task (tests and shutdown).

        :param job_id: Job id.
        """
        task = self._tasks.get(job_id)
        if task is not None:
            await task

    async def _run(
        self,
        job: ContextJob,
        service: ContextService,
        gather_sessions: SessionGatherer,
        *,
        code_graph: bool,
        proposals: bool,
    ) -> None:
        """Execute the steps, recording progress on ``job``.

        :param job: The job to update.
        :param service: The project's context service.
        :param gather_sessions: Transcript gatherer.
        :param code_graph: Whether to run the code graph step.
        :param proposals: Whether to run the proposals step.
        """
        failed = False
        try:
            if not await self._step_code_graph(job, service, enabled=code_graph):
                failed = True
            consumed = await self._step_proposals(job, service, gather_sessions, enabled=proposals)
            if consumed is None:
                failed = True
            stamp = job.step("stamp")
            if consumed:
                await asyncio.to_thread(service.write_last_update, job.inputs_gathered_at)
                stamp.status, stamp.detail = "succeeded", "recorded .last_update"
            else:
                stamp.status = "skipped"
                stamp.detail = "inputs not consumed; next run reconsiders them"
        except Exception as exc:  # surfaced on the job, never raised
            _logger.exception("context update %s crashed", job.id)
            job.note(f"unexpected error: {exc}")
            job.error = str(exc)
            failed = True
        finally:
            job.status = "failed" if failed else "succeeded"
            if failed and job.error is None:
                job.error = "; ".join(
                    f"{s.name}: {s.detail}" for s in job.steps if s.status == "failed"
                )
            job.finished_at = int(time.time())
            job.note(f"finished: {job.status}")
            self._tasks.pop(job.id, None)

    async def _step_code_graph(
        self, job: ContextJob, service: ContextService, *, enabled: bool
    ) -> bool:
        """Run the graphify step.

        :param job: The job.
        :param service: The context service.
        :param enabled: Whether the caller asked for this step.
        :returns: ``False`` only when the step failed.
        """
        step = job.step("code_graph")
        repo = service.config.repo_path
        if not enabled:
            step.status, step.detail = "skipped", "not requested"
            return True
        if not repo:
            step.status, step.detail = "skipped", "no repo_path configured"
            return True
        if not Path(repo).is_dir():
            step.status, step.detail = "failed", f"repo_path {repo} is not a directory"
            job.note(step.detail)
            return False
        step.status = "running"
        job.note(f"building code graph for {repo}")
        try:
            copied = await asyncio.to_thread(
                self._code_graph_runner, Path(repo), service.root / "graph", job.note
            )
        except Exception as exc:  # noqa: BLE001 — step failure, job continues
            step.status, step.detail = "failed", str(exc)
            job.note(f"code graph failed: {exc}")
            return False
        step.status, step.detail = "succeeded", f"updated {copied} file(s) in graph/"
        return True

    async def _step_proposals(
        self,
        job: ContextJob,
        service: ContextService,
        gather_sessions: SessionGatherer,
        *,
        enabled: bool,
    ) -> bool | None:
        """Run the curator step.

        :param job: The job.
        :param service: The context service.
        :param gather_sessions: Transcript gatherer.
        :param enabled: Whether the caller asked for this step.
        :returns: ``True`` when inputs were consumed (stamp may advance),
            ``False`` when skipped without consuming, ``None`` on failure.
        """
        step = job.step("proposals")
        if not enabled:
            step.status, step.detail = "skipped", "not requested"
            return False
        step.status = "running"
        since = service.last_update()
        # The stamp records when inputs were *gathered*, so anything that
        # changes while the curator runs is reconsidered next time.
        job.inputs_gathered_at = time.time()
        raw_inputs = await asyncio.to_thread(gather_raw_inputs, service, since)
        try:
            # Session timestamps are whole seconds and the filter is inclusive;
            # round up so the sessions reviewed last time are not re-sent.
            session_inputs = await gather_sessions(math.ceil(since) if since is not None else None)
        except Exception as exc:  # noqa: BLE001 — step failure, job continues
            step.status, step.detail = "failed", f"could not read sessions: {exc}"
            job.note(step.detail)
            return None
        inputs = raw_inputs + session_inputs
        job.note(f"{len(raw_inputs)} raw file(s) and {len(session_inputs)} session(s) to review")
        if not inputs:
            step.status, step.detail = "succeeded", "nothing new since the last update"
            return True
        curator = self._curator_factory()
        if curator is None:
            step.status = "skipped"
            step.detail = "no curator available (install the claude CLI)"
            job.note(step.detail)
            return False
        context_md_path = service.root / "CONTEXT.md"
        request = CuratorRequest(
            context_md=(
                context_md_path.read_text(encoding="utf-8") if context_md_path.is_file() else ""
            ),
            files=await asyncio.to_thread(_current_files, service),
            inputs=inputs,
        )
        job.note("running curator")
        try:
            drafts = await curator.propose(request)
        except Exception as exc:  # noqa: BLE001 — step failure, job continues
            step.status, step.detail = "failed", f"curator failed: {exc}"
            job.note(step.detail)
            return None
        store = ProposalStore(service)
        created = 0
        for draft in drafts:
            reason = await asyncio.to_thread(
                validate_draft, service, draft.path, draft.action, draft.new_content
            )
            if reason is not None:
                job.note(f"dropped proposal for {draft.path}: {reason}")
                continue
            resolved = service.resolve(draft.path)
            record = {
                "path": resolved.rel,
                "action": draft.action,
                "new_content": draft.new_content,
                "rationale": draft.rationale,
                "sources": draft.sources,
                "base_sha": await asyncio.to_thread(service.current_sha, resolved.rel),
                "job_id": job.id,
            }
            await asyncio.to_thread(store.save, record)
            created += 1
        job.proposals_created = created
        step.status, step.detail = "succeeded", f"{created} proposal(s) ready for review"
        return True
