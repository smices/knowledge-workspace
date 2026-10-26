"""Application policy around the vendored base-plat Global ID client."""

import re

from app.vendor.global_id import GlobalIdClient

MAX_INT64 = 9223372036854775807


def validate_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
            or int(value) > MAX_INT64):
        raise ValueError("business ID must be a canonical positive int64 string")
    return value


class GlobalIDUnavailable(RuntimeError):
    """The shared ID authority could not provide a valid business ID."""


class _SnowflakeV2Client(GlobalIdClient):
    @staticmethod
    def _validate(result, count):
        if not isinstance(result, dict) or result.get("algorithm") != "snowflake-v2":
            raise RuntimeError("Invalid Global ID algorithm")
        return GlobalIdClient._validate(result, count)


_client = None


def new_ids(count=1):
    global _client
    if type(count) is not int or count < 0:
        raise ValueError("count must be a non-negative integer")
    if count == 0:
        return []
    try:
        if _client is None:
            _client = _SnowflakeV2Client()
        ids = []
        while len(ids) < count:
            ids.extend(_client.next(min(1000, count - len(ids))))
        if len(set(ids)) != count:
            raise RuntimeError("duplicate Global IDs")
        return ids
    except Exception:
        raise GlobalIDUnavailable("Global ID authority unavailable") from None


def new_id():
    return new_ids(1)[0]
