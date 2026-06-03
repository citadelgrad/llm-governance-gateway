from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis

LUA_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local counter_key = key .. ':seq'

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

local seq = redis.call('INCR', counter_key)

redis.call('ZADD', key, now, tostring(seq))

local count = redis.call('ZCOUNT', key, now - window, '+inf')

redis.call('EXPIRE', key, math.ceil(window / 1000) + 1)
redis.call('EXPIRE', counter_key, math.ceil(window / 1000) + 1)

if count <= limit then
    return {1, limit - count}
else
    redis.call('ZREM', key, tostring(seq))
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 0
    if #oldest > 0 then
        retry_after = math.ceil((tonumber(oldest[2]) + window - now) / 1000)
    end
    return {0, retry_after}
end
"""


@dataclass
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int
    limit: int
    remaining: int


class RateLimiter:
    def __init__(self, redis: Redis, limit: int, window_seconds: int) -> None:
        self._redis = redis
        self._limit = limit
        self._window_ms = window_seconds * 1000
        self._sha: str = ""

    async def load_script(self) -> None:
        """Register the Lua script once at lifespan startup; avoids per-call script transfer."""
        self._sha = await self._redis.script_load(LUA_SCRIPT)

    async def check(self, user_id: str) -> RateLimitResult:
        now_ms = int(time.time() * 1000)
        key = f"rl:{user_id}"
        result = await cast(Any, self._redis.evalsha)(
            self._sha, 1, key,
            str(now_ms), str(self._window_ms), str(self._limit),
        )
        allowed, retry_after = result
        remaining = int(result[1]) if allowed else 0
        return RateLimitResult(
            allowed=bool(allowed),
            retry_after_seconds=int(retry_after),
            limit=self._limit,
            remaining=remaining,
        )
