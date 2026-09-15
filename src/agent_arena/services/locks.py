import asyncio
import uuid

# Shared in-process team locks to synchronize concurrent requests for the same team across all services
_team_locks: dict[uuid.UUID, asyncio.Lock] = {}
_team_locks_guard = asyncio.Lock()


async def get_team_lock(team_id: uuid.UUID) -> asyncio.Lock:
    """Returns the shared asyncio.Lock for the given team_id, creating one if needed."""
    async with _team_locks_guard:
        if team_id not in _team_locks:
            _team_locks[team_id] = asyncio.Lock()
        return _team_locks[team_id]
