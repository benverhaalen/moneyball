"""Opaque, bounded pagination tokens pinned to evidence and query identity."""
import base64
import json
from .store import DataError, timestamp


def encode_cursor(binding, offset, cutoff, state=None):
    body = {"v": 1, "binding": binding, "offset": offset, "cutoff": cutoff, "state":state}
    return base64.urlsafe_b64encode(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).decode()


def decode_cursor(token, binding):
    try:
        if not isinstance(token, str) or len(token) > 131072:
            raise ValueError()
        body = json.loads(base64.b64decode(token, altchars=b'-_', validate=True))
        if not isinstance(body, dict):
            raise ValueError()
        if (body.get("v") != 1 or body.get("binding") != binding or
                isinstance(body.get("offset"), bool) or not isinstance(body.get("offset"), int) or
                not 0 <= body["offset"] <= 1000000 or
                isinstance(body.get("cutoff"), bool) or not isinstance(body.get("cutoff"), (int, float))):
            raise ValueError()
        return body["offset"], timestamp(body["cutoff"]), body.get("state")
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DataError("Invalid pagination cursor or changed query; restart the query") from None
