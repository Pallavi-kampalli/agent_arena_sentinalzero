import asyncio
import uuid

# Shared in-process locks to synchronize concurrent requests for the same team / submission across all services
_team_locks: dict[uuid.UUID, asyncio.Lock] = {}
_submission_locks: dict[uuid.UUID, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def get_team_lock(team_id: uuid.UUID) -> asyncio.Lock:
    """Returns the shared asyncio.Lock for the given team_id, creating one if needed."""
    async with _locks_guard:
        if team_id not in _team_locks:
            _team_locks[team_id] = asyncio.Lock()
        return _team_locks[team_id]


async def get_submission_lock(submission_id: uuid.UUID) -> asyncio.Lock:
    """Returns the shared asyncio.Lock for the given submission_id, creating one if needed."""
    async with _locks_guard:
        if submission_id not in _submission_locks:
            _submission_locks[submission_id] = asyncio.Lock()
        return _submission_locks[submission_id]
