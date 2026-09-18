import csv
import io
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.logging import logger
from agent_arena.models.setting import Setting, SettingsAuditLog
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.schemas.admin import (
    AdminAuditLogItem,
    AdminHealthResponse,
    AdminLeaderboardEntry,
    AdminPhaseResponse,
    AdminScoreTriggerResponse,
    AdminSettingItem,
    AdminSettingsResponse,
    AdminSettingUpdateResponse,
    AdminSubmissionDetailResponse,
    AdminSubmissionSummary,
    AdminTeamBulkImportRequest,
    AdminTeamBulkImportResponse,
    AdminTeamCreateRequest,
    AdminTeamCreateResponse,
    AdminTeamDetailResponse,
    AdminTeamSummary,
    AdminTeamUpdateRequest,
    AdminTokenRegenerateResponse,
    AdminToolLogSummary,
)
from agent_arena.schemas.settings import PHASE_ORDER, CompetitionPhaseEnum
from agent_arena.scoring.service import ScoringService
from agent_arena.services.auth_service import regenerate_team_token, register_team
from agent_arena.services.locks import get_team_lock
from agent_arena.services.settings_service import SettingsService


class AdminService:
    """Orchestrates administrative control plane operations.

    Provides team management, runtime settings updates, competition lifecycle control,
    live leaderboard ranking with tiebreaks, force rescoring, and monitoring.
    Delegates to canonical domain services and preserves Phase 0-4 invariants.
    """

    def __init__(self, session: AsyncSession, settings_service: SettingsService | None = None) -> None:
        self.session = session
        self.settings_service = settings_service or SettingsService(session)

    # --------------------------------------------------------------------------
    # 1. Team Management
    # --------------------------------------------------------------------------

    async def list_teams(
        self,
        search: str | None = None,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AdminTeamSummary], int]:
        """Lists registered teams with optional search, status filtering, and pagination."""
        query = sa.select(Team)

        if search:
            query = query.where(Team.team_name.ilike(f"%{search.strip()}%"))
        if status_filter:
            query = query.where(Team.status == status_filter)

        # Count total
        count_query = sa.select(sa.func.count()).select_from(query.subquery())
        total = (await self.session.execute(count_query)).scalar_one() or 0

        # Order and paginate
        query = query.order_by(Team.created_at.desc()).offset(offset).limit(limit)
        teams = list((await self.session.execute(query)).scalars().all())

        # Load submission counts and latest scores for summary
        team_summaries = []
        for team in teams:
            sub_query = sa.select(
                sa.func.count(Submission.submission_id),
                sa.func.max(Submission.aggregate_score),
            ).where(Submission.team_id == team.team_id)
            sub_count, latest_score = (await self.session.execute(sub_query)).one()
            team_summaries.append(
                AdminTeamSummary(
                    team_id=str(team.team_id),
                    team_name=team.team_name,
                    display_id=team.display_id,
                    team_code=team.team_code,
                    status=team.status,
                    token_version=team.token_version,
                    github_repo_url=team.github_repo_url,
                    members=team.members,
                    created_at=team.created_at,
                    submissions_count=int(sub_count or 0),
                    latest_score=float(latest_score) if latest_score is not None else None,
                )
            )

        return team_summaries, total

    async def create_team(self, req: AdminTeamCreateRequest) -> AdminTeamCreateResponse:
        """Registers a new team, stores hashed token, and formats ready-to-paste .env snippet."""
        # Validate unique team name
        existing = (
            await self.session.execute(sa.select(Team).where(Team.team_name == req.team_name))
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "TEAM_NAME_EXISTS", "message": f"Team '{req.team_name}' already exists."},
            )

        expiry_hours = await self.settings_service.get("bearer_token_expiry_hours", None)
        team, raw_token = await register_team(
            self.session,
            team_name=req.team_name,
            members=req.members,
            github_repo_url=req.github_repo_url,
            expiry_hours=int(expiry_hours) if expiry_hours is not None else None,
        )

        arena_url = get_config().public_arena_url
        env_snippet = (
            f"SUBMISSION_ARENA_URL={arena_url}\n"
            f"SUBMISSION_TEAM_ID={team.team_id}\n"
            f"SUBMISSION_TEAM_CODE={team.team_code}\n"
            f"SUBMISSION_BEARER_TOKEN={raw_token}\n"
        )

        logger.info(
            f"Admin registered team '{team.team_name}'",
            extra={"team_id": str(team.team_id), "team_name": team.team_name},
        )

        return AdminTeamCreateResponse(
            team_id=str(team.team_id),
            team_name=team.team_name,
            display_id=team.display_id,
            team_code=team.team_code,
            status=team.status,
            token_version=team.token_version,
            bearer_token=raw_token,
            token=raw_token,
            env_snippet=env_snippet,
            created_at=team.created_at,
        )

    async def bulk_import_teams(self, req: AdminTeamBulkImportRequest) -> AdminTeamBulkImportResponse:
        """Bulk registers teams from CSV text or structured list, reporting row-level errors."""
        teams_to_import: list[AdminTeamCreateRequest] = []
        total_rows = 0

        if req.teams:
            teams_to_import.extend(req.teams)
            total_rows += len(req.teams)

        if req.csv_content:
            f = io.StringIO(req.csv_content.strip())
            reader = csv.DictReader(f)
            fieldnames = [fn.strip() for fn in (reader.fieldnames or [])]
            if "team_name" not in fieldnames:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "INVALID_CSV_HEADER", "message": "CSV must contain a 'team_name' header."},
                )

            for row in reader:
                total_rows += 1
                name = row.get("team_name") or row.get("name")
                if not name or not name.strip():
                    continue
                members_raw = row.get("members") or row.get("member_names")
                members_val: list[str] | None = None
                if members_raw:
                    members_val = [m.strip() for m in members_raw.split(";") if m.strip()]
                teams_to_import.append(
                    AdminTeamCreateRequest(
                        team_name=name.strip(),
                        members=members_val,
                        github_repo_url=row.get("github_repo_url") or row.get("repo_url"),
                    )
                )

        created: list[AdminTeamCreateResponse] = []
        failed: list[dict[str, Any]] = []
        skipped_count = 0

        for item in teams_to_import:
            try:
                res = await self.create_team(item)
                created.append(res)
            except HTTPException as e:
                if e.status_code == status.HTTP_409_CONFLICT:
                    skipped_count += 1
                else:
                    failed.append({"team_name": item.team_name, "error": str(e.detail)})
            except Exception as e:
                failed.append({"team_name": item.team_name, "error": str(e)})

        return AdminTeamBulkImportResponse(
            created_teams=created,
            teams=created,
            failed_rows=failed,
            total_imported=len(created),
            total_rows=total_rows,
            created_count=len(created),
            skipped_count=skipped_count,
        )

    async def get_team_detail(self, team_id: uuid.UUID) -> AdminTeamDetailResponse:
        """Retrieves comprehensive team profile and submission history."""
        team = (await self.session.execute(sa.select(Team).where(Team.team_id == team_id))).scalar_one_or_none()
        if team is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "TEAM_NOT_FOUND", "message": f"Team {team_id} not found."},
            )

        # Fetch submissions
        sub_stmt = sa.select(Submission).where(Submission.team_id == team_id).order_by(Submission.attempt_number.asc())
        subs = list((await self.session.execute(sub_stmt)).scalars().all())
        sub_dicts = [
            {
                "submission_id": str(s.submission_id),
                "attempt_number": s.attempt_number,
                "status": s.status,
                "aggregate_score": float(s.aggregate_score) if s.aggregate_score is not None else None,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            }
            for s in subs
        ]

        return AdminTeamDetailResponse(
            team_id=str(team.team_id),
            team_name=team.team_name,
            display_id=team.display_id,
            team_code=team.team_code,
            status=team.status,
            token_version=team.token_version,
            github_repo_url=team.github_repo_url,
            members=team.members,
            created_at=team.created_at,
            updated_at=team.updated_at,
            submissions=sub_dicts,
        )

    async def update_team(self, team_id: uuid.UUID, req: AdminTeamUpdateRequest) -> AdminTeamDetailResponse:
        """Updates team metadata (name, members, github repository)."""
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            team = (await self.session.execute(sa.select(Team).where(Team.team_id == team_id))).scalar_one_or_none()
            if team is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "TEAM_NOT_FOUND", "message": f"Team {team_id} not found."},
                )

            if req.team_name is not None and req.team_name != team.team_name:
                existing = (
                    await self.session.execute(sa.select(Team).where(Team.team_name == req.team_name))
                ).scalar_one_or_none()
                if existing is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"error": "TEAM_NAME_EXISTS", "message": f"Team '{req.team_name}' already exists."},
                    )
                team.team_name = req.team_name

            if req.members is not None:
                team.members = req.members
            if req.github_repo_url is not None:
                team.github_repo_url = req.github_repo_url

            team.updated_at = datetime.now(UTC)
            await self.session.commit()
            await self.session.refresh(team)

            return await self.get_team_detail(team_id)

    async def update_team_status(
        self, team_id: uuid.UUID, new_status: str, actor: str = "admin"
    ) -> AdminTeamDetailResponse:
        """Suspends, activates, or disqualifies a team."""
        if new_status not in {"active", "suspended", "disqualified"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_STATUS", "message": "Status must be active, suspended, or disqualified."},
            )

        team_lock = await get_team_lock(team_id)
        async with team_lock:
            team = (await self.session.execute(sa.select(Team).where(Team.team_id == team_id))).scalar_one_or_none()
            if team is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "TEAM_NOT_FOUND", "message": f"Team {team_id} not found."},
                )

            old_status = team.status
            team.status = new_status
            team.updated_at = datetime.now(UTC)

            # Record in settings_audit_log for unified auditability
            audit_log = SettingsAuditLog(
                key=f"team:{team_id}:status",
                old_value={"status": old_status},
                new_value={"status": new_status},
                changed_by=actor,
            )
            self.session.add(audit_log)
            await self.session.commit()
            await self.session.refresh(team)

            logger.info(
                f"Admin '{actor}' changed team '{team.team_name}' status from '{old_status}' to '{new_status}'",
                extra={"team_id": str(team_id), "actor": actor, "old_status": old_status, "new_status": new_status},
            )

            return await self.get_team_detail(team_id)

    async def regenerate_team_token(self, team_id: uuid.UUID, actor: str = "admin") -> AdminTokenRegenerateResponse:
        """Atomically bumps token_version, invalidating previous tokens on the next request."""
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            team = (await self.session.execute(sa.select(Team).where(Team.team_id == team_id))).scalar_one_or_none()
            if team is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "TEAM_NOT_FOUND", "message": f"Team {team_id} not found."},
                )

            old_version = team.token_version
            expiry_hours = await self.settings_service.get("bearer_token_expiry_hours", None)
            new_raw_token = await regenerate_team_token(
                self.session,
                team=team,
                expiry_hours=int(expiry_hours) if expiry_hours is not None else None,
            )

            # Record in settings_audit_log
            audit_log = SettingsAuditLog(
                key=f"team:{team_id}:token_regenerate",
                old_value={"token_version": old_version},
                new_value={"token_version": team.token_version},
                changed_by=actor,
            )
            self.session.add(audit_log)
            await self.session.commit()

            arena_url = get_config().public_arena_url
            env_snippet = (
                f"SUBMISSION_ARENA_URL={arena_url}\n"
                f"SUBMISSION_TEAM_ID={team.team_id}\n"
                f"SUBMISSION_TEAM_CODE={team.team_code}\n"
                f"SUBMISSION_BEARER_TOKEN={new_raw_token}\n"
            )

            logger.info(
                f"Admin '{actor}' regenerated token for team '{team.team_name}' (version {old_version} -> {team.token_version})",
                extra={"team_id": str(team_id), "actor": actor, "token_version": team.token_version},
            )

            return AdminTokenRegenerateResponse(
                team_id=str(team.team_id),
                team_name=team.team_name,
                token_version=team.token_version,
                bearer_token=new_raw_token,
                token=new_raw_token,
                env_snippet=env_snippet,
            )

    # --------------------------------------------------------------------------
    # 2. Settings & Competition Lifecycle
    # --------------------------------------------------------------------------

    async def get_all_settings(self) -> AdminSettingsResponse:
        """Returns all live competition settings with metadata."""
        merged_dict = await self.settings_service.get_all()
        # Fetch updated_at/by from DB for each key
        db_settings = {s.key: s for s in (await self.session.execute(sa.select(Setting))).scalars().all()}

        items = []
        for k, v in merged_dict.items():
            db_s = db_settings.get(k)
            items.append(
                AdminSettingItem(
                    key=k,
                    value=v,
                    updated_by=db_s.updated_by if db_s else "default",
                    updated_at=db_s.updated_at if db_s else None,
                )
            )

        return AdminSettingsResponse(settings=items)

    async def update_setting(self, key: str, value: Any, actor: str = "admin") -> AdminSettingUpdateResponse:
        """Updates a single setting with validation and audit logging."""
        # Read current value for response
        curr_res = await self.session.execute(sa.select(Setting).where(Setting.key == key))
        curr_s = curr_res.scalar_one_or_none()
        old_val = curr_s.value if curr_s else await self.settings_service.get(key)

        try:
            new_val = await self.settings_service.set(key=key, value=value, changed_by=actor)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_SETTING_VALUE", "message": str(e)},
            )

        return AdminSettingUpdateResponse(
            key=key,
            old_value=old_val,
            new_value=new_val,
            changed_by=actor,
            changed_at=datetime.now(UTC),
        )

    async def get_audit_logs(
        self, key: str | None = None, offset: int = 0, limit: int = 50
    ) -> tuple[list[AdminAuditLogItem], int]:
        """Queries immutable audit logs with optional key filter and pagination."""
        query = sa.select(SettingsAuditLog)
        if key:
            query = query.where(SettingsAuditLog.key == key)

        count_query = sa.select(sa.func.count()).select_from(query.subquery())
        total = (await self.session.execute(count_query)).scalar_one() or 0

        query = (
            query.order_by(SettingsAuditLog.changed_at.desc(), SettingsAuditLog.id.desc()).offset(offset).limit(limit)
        )
        logs = list((await self.session.execute(query)).scalars().all())

        items = [
            AdminAuditLogItem(
                id=int(log_entry.id),
                key=str(log_entry.key),
                old_value=log_entry.old_value,
                new_value=log_entry.new_value,
                changed_by=str(log_entry.changed_by),
                changed_at=log_entry.changed_at,
            )
            for log_entry in logs
        ]
        return items, total

    async def get_competition_phase(self) -> AdminPhaseResponse:
        """Returns current competition phase and valid forward transitions."""
        current_phase = await self.settings_service.get("competition_phase", "registration")
        start_at = await self.settings_service.get("competition_start_at", None)
        end_at = await self.settings_service.get("competition_end_at", None)

        try:
            curr_enum = CompetitionPhaseEnum(current_phase)
            curr_idx = PHASE_ORDER.index(curr_enum)
            allowed_next = [p.value for p in PHASE_ORDER[curr_idx + 1 :]]
        except ValueError:
            allowed_next = []

        return AdminPhaseResponse(
            current_phase=current_phase,
            allowed_next_phases=allowed_next,
            competition_start_at=start_at,
            competition_end_at=end_at,
        )

    async def transition_competition_phase(self, new_phase: str, actor: str = "admin") -> AdminPhaseResponse:
        """Transitions competition phase forward, enforcing finite state machine."""
        try:
            await self.settings_service.set("competition_phase", new_phase, changed_by=actor)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "INVALID_PHASE_TRANSITION", "message": str(e)},
            )
        return await self.get_competition_phase()

    # --------------------------------------------------------------------------
    # 3. Live Leaderboard & Tiebreaks
    # --------------------------------------------------------------------------

    async def get_leaderboard(self, mode: str = "best") -> tuple[list[AdminLeaderboardEntry], list[str]]:
        """Computes live leaderboard with canonical 4-tier tiebreaks.

        Tiebreak Order (PRD §6.2):
          1. aggregate_score DESC
          2. task_success DESC
          3. policy DESC
          4. earliest completed_at ASC
        """
        if mode not in {"best", "latest"}:
            mode = "best"

        tiebreak_order = ["aggregate_score DESC", "task_success DESC", "policy DESC", "completed_at ASC"]

        # Fetch all teams (exclude disqualified teams from competition leaderboard)
        teams = list((await self.session.execute(sa.select(Team).where(Team.status != "disqualified"))).scalars().all())
        entries: list[AdminLeaderboardEntry] = []

        for team in teams:
            # Query submissions
            sub_query = (
                sa.select(Submission)
                .where(Submission.team_id == team.team_id)
                .where(Submission.aggregate_score.is_not(None))
            )
            if mode == "latest":
                sub_query = sub_query.order_by(Submission.attempt_number.desc())
            else:
                # best
                sub_query = sub_query.order_by(Submission.aggregate_score.desc(), Submission.completed_at.asc())

            selected_sub = (await self.session.execute(sub_query)).scalars().first()

            # Count total submissions
            sub_count = (
                await self.session.execute(
                    sa.select(sa.func.count(Submission.submission_id)).where(Submission.team_id == team.team_id)
                )
            ).scalar_one() or 0

            if selected_sub is not None:
                dim_breakdown = (selected_sub.breakdown or {}).get("dimensions", {})
                agg_score = float(selected_sub.aggregate_score) if selected_sub.aggregate_score is not None else 0.0
                sub_metrics = (selected_sub.breakdown or {}).get("submission_metrics", {})
                duration_sec = sub_metrics.get("total_duration_seconds")
                if duration_sec is None and selected_sub.completed_at and selected_sub.started_at:
                    duration_sec = round((selected_sub.completed_at - selected_sub.started_at).total_seconds(), 2)
                tool_calls_tot = sub_metrics.get("tool_calls_total")

                entries.append(
                    AdminLeaderboardEntry(
                        rank=0,  # calculated after sort
                        team_id=str(team.team_id),
                        team_name=team.team_name,
                        team_code=team.team_code,
                        status=team.status,
                        aggregate_score=agg_score,
                        task_success=float(dim_breakdown.get("task_success", 0.0)),
                        policy=float(dim_breakdown.get("policy", 0.0)),
                        robustness=float(dim_breakdown.get("robustness", 0.0)),
                        evidence=float(dim_breakdown.get("evidence", 0.0)),
                        calibration=float(dim_breakdown.get("calibration", 0.0)),
                        efficiency=float(dim_breakdown.get("efficiency", 0.0)),
                        communication=float(dim_breakdown.get("communication", 0.0)),
                        submissions_count=int(sub_count),
                        duration_seconds=float(duration_sec) if duration_sec is not None else None,
                        tool_calls_total=int(tool_calls_tot) if tool_calls_tot is not None else None,
                        last_submission_at=selected_sub.completed_at,
                    )
                )
            else:
                # Team registered but has no scored submissions
                entries.append(
                    AdminLeaderboardEntry(
                        rank=0,
                        team_id=str(team.team_id),
                        team_name=team.team_name,
                        team_code=team.team_code,
                        status=team.status,
                        aggregate_score=0.0,
                        task_success=0.0,
                        policy=0.0,
                        robustness=0.0,
                        evidence=0.0,
                        calibration=0.0,
                        efficiency=0.0,
                        communication=0.0,
                        submissions_count=int(sub_count),
                        duration_seconds=None,
                        tool_calls_total=None,
                        last_submission_at=None,
                    )
                )

        # Sort according to canonical tiebreak rules
        def sort_key(e: AdminLeaderboardEntry) -> tuple[float, float, float, float]:
            ts = e.last_submission_at.timestamp() if e.last_submission_at else float("inf")
            return (-e.aggregate_score, -e.task_success, -e.policy, ts)

        entries.sort(key=sort_key)

        # Assign ranks
        for idx, entry in enumerate(entries, start=1):
            entry.rank = idx

        return entries, tiebreak_order

    async def export_leaderboard_csv(self, mode: str = "best") -> str:
        """Generates RFC 4180 compliant CSV string of the live leaderboard."""
        entries, _ = await self.get_leaderboard(mode=mode)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Rank",
                "Team Name",
                "Team Code",
                "Team ID",
                "Status",
                "Duration (s)",
                "Aggregate Score",
                "Task Success",
                "Policy",
                "Robustness",
                "Evidence",
                "Calibration",
                "Efficiency",
                "Communication",
                "Tool Calls Total",
                "Submissions Count",
                "Last Submission At",
            ]
        )
        for e in entries:
            writer.writerow(
                [
                    e.rank,
                    e.team_name,
                    e.team_code or "",
                    e.team_id,
                    e.status,
                    f"{e.duration_seconds:.1f}" if e.duration_seconds is not None else "",
                    f"{e.aggregate_score:.4f}",
                    f"{e.task_success:.4f}",
                    f"{e.policy:.4f}",
                    f"{e.robustness:.4f}",
                    f"{e.evidence:.4f}",
                    f"{e.calibration:.4f}",
                    f"{e.efficiency:.4f}",
                    f"{e.communication:.4f}",
                    e.tool_calls_total if e.tool_calls_total is not None else "",
                    e.submissions_count,
                    e.last_submission_at.isoformat() if e.last_submission_at else "",
                ]
            )
        return output.getvalue()

    # --------------------------------------------------------------------------
    # 4. Submissions & Scoring Controls
    # --------------------------------------------------------------------------

    async def list_submissions(
        self,
        team_id: uuid.UUID | None = None,
        status_filter: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AdminSubmissionSummary], int]:
        """Lists submissions with optional team and status filters."""
        query = sa.select(Submission, Team.team_name, Team.team_code).join(Team, Team.team_id == Submission.team_id)
        if team_id:
            query = query.where(Submission.team_id == team_id)
        if status_filter:
            query = query.where(Submission.status == status_filter)

        count_query = sa.select(sa.func.count()).select_from(query.subquery())
        total = (await self.session.execute(count_query)).scalar_one() or 0

        query = query.order_by(Submission.started_at.desc()).offset(offset).limit(limit)
        results = (await self.session.execute(query)).all()

        summaries = []
        for sub, team_name, team_code in results:
            sub_metrics = (sub.breakdown or {}).get("submission_metrics", {})
            duration_sec = sub_metrics.get("total_duration_seconds")
            if duration_sec is None and sub.completed_at and sub.started_at:
                duration_sec = round((sub.completed_at - sub.started_at).total_seconds(), 2)
            tool_calls_count = sub_metrics.get("tool_calls_total")

            summaries.append(
                AdminSubmissionSummary(
                    submission_id=str(sub.submission_id),
                    team_id=str(sub.team_id),
                    team_name=team_name,
                    team_code=team_code,
                    attempt_number=sub.attempt_number,
                    status=sub.status,
                    aggregate_score=float(sub.aggregate_score) if sub.aggregate_score is not None else None,
                    duration_seconds=float(duration_sec) if duration_sec is not None else None,
                    tool_calls_count=int(tool_calls_count) if tool_calls_count is not None else None,
                    started_at=sub.started_at,
                    completed_at=sub.completed_at,
                )
            )
        return summaries, total

    async def get_submission_detail(self, submission_id: uuid.UUID) -> AdminSubmissionDetailResponse:
        """Retrieves detailed submission scores, dimensions, and per-task audit entries."""
        stmt = (
            sa.select(Submission, Team.team_name, Team.team_code)
            .join(Team, Team.team_id == Submission.team_id)
            .where(Submission.submission_id == submission_id)
        )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission {submission_id} not found."},
            )

        sub, team_name, team_code = row
        sub_metrics = (sub.breakdown or {}).get("submission_metrics", {})
        duration_sec = sub_metrics.get("total_duration_seconds")
        if duration_sec is None and sub.completed_at and sub.started_at:
            duration_sec = round((sub.completed_at - sub.started_at).total_seconds(), 2)
        tool_calls_count = sub_metrics.get("tool_calls_total")
        tool_calls_breakdown = sub_metrics.get("tool_calls_breakdown")

        return AdminSubmissionDetailResponse(
            submission_id=str(sub.submission_id),
            team_id=str(sub.team_id),
            team_name=team_name,
            team_code=team_code,
            attempt_number=sub.attempt_number,
            status=sub.status,
            aggregate_score=float(sub.aggregate_score) if sub.aggregate_score is not None else None,
            duration_seconds=float(duration_sec) if duration_sec is not None else None,
            tool_calls_count=int(tool_calls_count) if tool_calls_count is not None else None,
            tool_calls_breakdown=tool_calls_breakdown,
            breakdown=sub.breakdown,
            per_task_results=sub.per_task_results,
            started_at=sub.started_at,
            completed_at=sub.completed_at,
        )

    async def trigger_submission_scoring(
        self, submission_id: uuid.UUID, force: bool = False, actor: str = "admin"
    ) -> AdminScoreTriggerResponse:
        """Invokes ScoringService to score or force-rescore a submission."""
        scoring_service = ScoringService(self.session, self.settings_service)
        try:
            score_result = await scoring_service.score_submission(submission_id=submission_id, force=force)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "SCORING_FAILED", "message": str(e)},
            )

        logger.info(
            f"Admin '{actor}' triggered scoring for submission {submission_id} (force={force}, score={score_result.aggregate_score})",
            extra={
                "submission_id": str(submission_id),
                "actor": actor,
                "force": force,
                "score": score_result.aggregate_score,
            },
        )

        score_run_id = (
            score_result.breakdown.get("scoring_metadata", {}).get("score_run_id")
            if isinstance(score_result.breakdown, dict)
            else None
        )

        return AdminScoreTriggerResponse(
            submission_id=str(score_result.submission_id),
            aggregate_score=score_result.aggregate_score,
            breakdown=score_result.breakdown,
            score_run_id=score_run_id,
            rescore_applied=force,
        )

    # --------------------------------------------------------------------------
    # 5. Monitoring & Operational Health
    # --------------------------------------------------------------------------

    async def get_tool_call_logs(
        self,
        team_id: uuid.UUID | None = None,
        task_id: str | None = None,
        tool_name: str | None = None,
        rejections_only: bool = False,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AdminToolLogSummary], int]:
        """Queries detailed tool execution logs for operational audit and dispute resolution."""
        query = sa.select(ToolCallLog)
        if team_id:
            query = query.where(ToolCallLog.team_id == team_id)
        if task_id:
            query = query.where(ToolCallLog.task_id == task_id)
        if tool_name:
            query = query.where(ToolCallLog.tool_name == tool_name)
        if rejections_only:
            query = query.where(ToolCallLog.was_enforcement_rejection.is_(True))

        count_query = sa.select(sa.func.count()).select_from(query.subquery())
        total = (await self.session.execute(count_query)).scalar_one() or 0

        query = query.order_by(ToolCallLog.created_at.desc()).offset(offset).limit(limit)
        logs = list((await self.session.execute(query)).scalars().all())

        summaries = [
            AdminToolLogSummary(
                id=int(log_item.id),
                team_id=str(log_item.team_id),
                task_id=log_item.task_id,
                submission_id=str(log_item.submission_id) if log_item.submission_id else None,
                tool_name=log_item.tool_name,
                was_enforcement_rejection=log_item.was_enforcement_rejection,
                latency_ms=log_item.latency_ms,
                created_at=log_item.created_at,
                request_payload=log_item.request_payload,
                response_payload=log_item.response_payload,
            )
            for log_item in logs
        ]
        return summaries, total

    async def get_system_health(self) -> AdminHealthResponse:
        """Computes live operational metrics including task pool exhaustion warnings."""
        # 1. DB connectivity
        db_status = "healthy"
        try:
            await self.session.execute(sa.text("SELECT 1"))
        except Exception:
            db_status = "unhealthy"

        # 2. Counts
        active_teams = (
            await self.session.execute(sa.select(sa.func.count(Team.team_id)).where(Team.status == "active"))
        ).scalar_one() or 0

        total_subs = (await self.session.execute(sa.select(sa.func.count(Submission.submission_id)))).scalar_one() or 0

        # Hidden tasks count
        hidden_tasks = (
            await self.session.execute(sa.select(sa.func.count(Task.task_id)).where(Task.dataset == "hidden"))
        ).scalar_one() or 0

        # Required capacity per team
        hidden_task_budget = await self.settings_service.get("hidden_task_count", 30)
        task_capacity = int(hidden_task_budget)

        # Warning condition: hidden tasks available is fewer than needed for active teams
        # Under Model B, tasks are reusable seeds across teams, but total seed diversity must satisfy capacity
        task_pool_warning = hidden_tasks < task_capacity

        # Average latency ms
        avg_latency = (await self.session.execute(sa.select(sa.func.avg(ToolCallLog.latency_ms)))).scalar_one() or 0.0

        current_phase = await self.settings_service.get("competition_phase", "registration")

        return AdminHealthResponse(
            status="healthy" if db_status == "healthy" else "degraded",
            database=db_status,
            competition_phase=current_phase,
            active_teams_count=int(active_teams),
            total_submissions_count=int(total_subs),
            hidden_tasks_available=int(hidden_tasks),
            hidden_tasks_capacity_per_team=task_capacity,
            task_pool_warning=task_pool_warning,
            average_tool_latency_ms=round(float(avg_latency), 2),
        )
