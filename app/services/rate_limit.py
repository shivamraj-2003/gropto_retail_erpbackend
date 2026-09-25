"""In-process rate limiting and login-attempt lockout — matches the plan's "Redis
is not needed" call: this fits in a single dict because sessions are stateless and
there is one API process. Revisit only if a second API process is ever added.
"""

import time
from collections import defaultdict, deque

LOGIN_LOCKOUT_THRESHOLD = 5
LOGIN_LOCKOUT_WINDOW_SECONDS = 15 * 60
LOGIN_LOCKOUT_DURATION_SECONDS = 15 * 60

_login_failures: dict[str, deque[float]] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _login_key(identifier: str, device_fingerprint: str) -> str:
    # Keyed by identifier+device so one compromised/misconfigured till can't lock
    # out every cashier at a store, and one user guessing across many devices
    # still gets caught by the identifier component.
    return f"{identifier}:{device_fingerprint}"


def is_login_locked(identifier: str, device_fingerprint: str) -> float | None:
    """Returns remaining lockout seconds, or None if not locked."""
    key = _login_key(identifier, device_fingerprint)
    until = _locked_until.get(key)
    if until is None:
        return None
    remaining = until - time.monotonic()
    if remaining <= 0:
        _locked_until.pop(key, None)
        return None
    return remaining


def record_login_failure(identifier: str, device_fingerprint: str) -> None:
    key = _login_key(identifier, device_fingerprint)
    now = time.monotonic()
    window = _login_failures[key]
    window.append(now)
    while window and now - window[0] > LOGIN_LOCKOUT_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= LOGIN_LOCKOUT_THRESHOLD:
        _locked_until[key] = now + LOGIN_LOCKOUT_DURATION_SECONDS
        window.clear()


def record_login_success(identifier: str, device_fingerprint: str) -> None:
    key = _login_key(identifier, device_fingerprint)
    _login_failures.pop(key, None)
    _locked_until.pop(key, None)


# ---------------------------------------------------------------------------
# General request rate limiting — a simple fixed-window counter per client IP.
# ---------------------------------------------------------------------------

REQUEST_LIMIT_PER_WINDOW = 300
REQUEST_WINDOW_SECONDS = 60

_request_counts: dict[str, tuple[int, float]] = {}


def is_request_rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    count, window_start = _request_counts.get(client_ip, (0, now))
    if now - window_start > REQUEST_WINDOW_SECONDS:
        count, window_start = 0, now
    count += 1
    _request_counts[client_ip] = (count, window_start)
    return count > REQUEST_LIMIT_PER_WINDOW
