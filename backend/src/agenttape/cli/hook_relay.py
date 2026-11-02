"""The SessionStart hook relay: carries one Claude Code hook's stdin JSON to AgentTape's
hooks endpoint, for the event Claude Code will not run as an ``http`` hook.

Claude Code skips every ``type: "http"`` hook on ``SessionStart`` (and ``Setup``); its
hook matcher logs "HTTP hooks are not supported for SessionStart". ``agenttape hooks
setup`` therefore installs SessionStart as an exec-form ``type: "command"`` hook: the
absolute interpreter plus a fixed argv (``-I -m agenttape.cli.hook_relay --data-dir <dir>
--url <url>``), spawned by Claude Code with no shell and in isolated mode, so the
session's working directory and ``PYTHON*`` environment cannot shadow a module. Run as
``__main__``, this module is that command. ``agenttape hooks relay`` is a manual alias.

It is its own entry point, not the CLI, because every SessionStart pays its start-up: it
imports only the standard library and ``security/client_token.py`` (the token reader,
imported lazily), never AgentTape's settings, pydantic, the CLI, or the API and database
stack, so nothing unrelated can fail at import time or slow a session's start.

The relay posts its stdin bytes unchanged to the same endpoint, with the same
``Authorization`` bearer token and ``X-AgentTape-Run-Id`` header the ``http`` hooks send,
so the server parses and ingests a relayed SessionStart through the one path an ``http``
hook takes. The token is read from AgentTape's owner-only token store
(``<data_dir>/capture/hooks-client.token``, 0600 in a 0700 directory); it is never written
into settings.json for this hook.

Fail open, always: run as a command, the relay's first statements point stdout and
stderr at the null device (a SessionStart command hook's stdout becomes model context)
and make any uncaught exception a silent exit 0, before anything else is imported; every
path then ends in ``os._exit(0)``, whether AgentTape is down, slow, or has no token, and
nothing runs past a hard deadline. It only ever posts to AgentTape's loopback hooks
endpoint, never through a proxy and never following a redirect.
"""

from __future__ import annotations

import os
import sys

if __name__ == "__main__" and sys.argv[1:2] != ["--probe"]:  # see probe()
    # Before anything else is imported: nothing this process does may reach stdout or
    # stderr, and nothing it raises may change its exit status.
    sys.excepthook = lambda *_: os._exit(0)
    _null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(_null, 1)
    os.dup2(_null, 2)

# Every import below runs after the silencing above, on purpose.
import contextlib
import ipaddress
import json
import re
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

#: Must equal ``capture.claude_settings.HOOK_URL_PATH`` and ``RUN_ID_HEADER`` (asserted by
#: a unit test); kept here so the relay imports nothing but the standard library.
HOOK_URL_PATH = "/api/v1/hooks/claude-code"
RUN_ID_HEADER = "X-AgentTape-Run-Id"

#: The relay's hard deadline for everything after start-up (reading stdin, reading the
#: token, the POST). Well inside ``claude_settings.RELAY_HOOK_TIMEOUT_SECONDS``, Claude
#: Code's own timeout for the hook, and the same bound the ``http`` hooks get.
RELAY_DEADLINE_SECONDS = 3.0
#: One byte past this is still forwarded (AgentTape answers 413 and counts the hook as
#: dropped); more is never read. Equals ``api.routers.hooks.MAX_HOOK_BODY_BYTES``.
MAX_RELAY_BODY_BYTES = 1024 * 1024
RUN_ID_ENV = "AGENTTAPE_RUN_ID"
#: ``--probe`` (``claude_settings.RELAY_PROBE_ARGS``) prints exactly this and exits 0 when
#: this interpreter can run the relay; ``hooks setup`` and ``hooks status`` check for it.
PROBE_ARG = "--probe"
PROBE_OUTPUT = b"agenttape-hook-relay: ok\n"
_READ_CHUNK = 64 * 1024
#: How the relay's last run ended, recorded in :data:`LAST_OUTCOME_FILENAME`. A
#: closed set: ``posted`` (2xx), ``rejected`` (any other HTTP status, e.g. 401 after a
#: token rotation, 413, 429, 503, or a refused redirect), ``unreachable`` (nothing
#: answered), ``timeout`` (the deadline passed), ``no_token``, ``empty_input``,
#: ``refused_url`` (not AgentTape's loopback hooks endpoint), ``error`` (anything else).
OUTCOMES = (
    "posted",
    "rejected",
    "unreachable",
    "timeout",
    "no_token",
    "empty_input",
    "refused_url",
    "error",
)
#: ``<data_dir>/capture/hook-relay-last.json`` (0600): the time, the outcome, and the HTTP
#: status, nothing else (never the token, never the hook's payload). Written only when
#: the capture directory already exists; the relay never creates a data dir.
LAST_OUTCOME_FILENAME = "hook-relay-last.json"
#: A relay killed between writing its temp record and renaming it (Claude Code's own
#: timeout) leaves ``.hook-relay-*.tmp`` behind; a later run removes ones older than this,
#: looking at no more than :data:`_MAX_TEMP_SCAN` directory entries.
_TEMP_PREFIX = ".hook-relay-"
_TEMP_SUFFIX = ".tmp"
_STALE_TEMP_SECONDS = 60.0
_MAX_TEMP_SCAN = 256
#: ``(outcome, http_status)``: one of :data:`OUTCOMES`, and the status when one arrived.
Outcome = tuple[str, int | None]
#: A run id worth sending: printable ASCII, no space, CR or LF, at most 128 characters.
#: Anything else is left out (the server would discard it anyway) rather than letting
#: ``http.client`` refuse the header and drop the whole SessionStart.
_SENDABLE_RUN_ID = re.compile(r"[\x21-\x7e]{1,128}")


class _NoRedirects(HTTPRedirectHandler):
    """A redirect would carry the bearer token to another URL: never follow one."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def _is_loopback_ip_literal(host: str) -> bool:
    """Whether ``host`` is a loopback IP literal. Host names (``localhost`` included) are
    refused: one resolves through ``/etc/hosts`` and DNS at post time, so the bearer token
    would go wherever it resolves, and ``hooks setup`` only ever writes an IP literal
    (``security.capture.capture_api_base``). An IPv6 zone id is refused too."""
    if "%" in host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def relay_url_allowed(url: str) -> bool:
    """Whether ``url`` is AgentTape's own hooks endpoint over plain HTTP on a loopback IP
    literal: the only place the relay will ever send the hooks token."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and host is not None
        and _is_loopback_ip_literal(host)
        and parts.username is None
        and parts.password is None
        and parts.path == HOOK_URL_PATH
        and not parts.query
        and not parts.fragment
    )


def _read_stdin(fd: int) -> bytes:
    """At most ``MAX_RELAY_BODY_BYTES + 1`` bytes from ``fd``, until EOF. A raw ``os.read``
    (no buffered stream, so no lock a stuck read could hold at interpreter exit)."""
    chunks: list[bytes] = []
    remaining = MAX_RELAY_BODY_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _relay_once(
    url: str, data_dir: Path, stdin_fd: int, environ: Mapping[str, str], timeout: float
) -> Outcome:
    if not relay_url_allowed(url):
        return ("refused_url", None)
    body = _read_stdin(stdin_fd)
    if not body.strip():
        return ("empty_input", None)
    # Imported here, inside the caller's catch-all, so even its import failing is silent.
    from agenttape.security.client_token import read_client_token

    token = read_client_token(data_dir, "hooks")
    if not token:
        return ("no_token", None)
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    run_id = environ.get(RUN_ID_ENV, "").strip()
    if _SENDABLE_RUN_ID.fullmatch(run_id):
        headers[RUN_ID_HEADER] = run_id
    opener = build_opener(ProxyHandler({}), _NoRedirects())
    request = Request(url, data=body, headers=headers, method="POST")  # noqa: S310 (loopback http, checked above)
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(_READ_CHUNK)
            return ("posted", response.status)
    except HTTPError as exc:  # a non-2xx answer, or a redirect _NoRedirects refused
        return ("rejected", exc.code)
    except TimeoutError:
        return ("timeout", None)
    except URLError as exc:
        return ("timeout" if isinstance(exc.reason, TimeoutError) else "unreachable", None)
    except OSError:
        return ("unreachable", None)


def relay(
    *,
    url: str,
    data_dir: Path,
    stdin_fd: int,
    environ: Mapping[str, str],
    deadline: float = RELAY_DEADLINE_SECONDS,
) -> Outcome:
    """Forward the hook body on ``stdin_fd`` to ``url``. Never raises. Returns how it
    ended; ``("timeout", None)`` also when the attempt was abandoned mid-way on a daemon
    thread at ``deadline``, which the caller must not wait for."""
    result: list[Outcome] = []

    def work() -> None:
        try:
            result.append(_relay_once(url, data_dir, stdin_fd, environ, deadline))
        except BaseException:
            result.append(("error", None))

    worker = threading.Thread(target=work, name="agenttape-hook-relay", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive() or not result:
        return ("timeout", None) if worker.is_alive() else ("error", None)
    return result[0]


def record_outcome(data_dir: Path, outcome: Outcome) -> None:
    """Replace :data:`LAST_OUTCOME_FILENAME` with ``outcome`` and the current UTC time
    (0600, written to a temporary file and renamed, so a concurrent session's relay never
    reads half a record). Does nothing when ``<data_dir>/capture`` does not exist: the relay
    never creates AgentTape's data dir. Never raises."""
    with contextlib.suppress(BaseException):
        capture = data_dir / "capture"
        if not capture.is_dir():
            return
        code, status = outcome
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "outcome": code if code in OUTCOMES else "error",
            "http_status": status if isinstance(status, int) else None,
        }
        _remove_stale_temps(capture)
        fd, tmp_name = tempfile.mkstemp(dir=capture, prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX)
        tmp = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, json.dumps(record).encode("ascii"))
            os.close(fd)
            tmp.replace(capture / LAST_OUTCOME_FILENAME)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            tmp.unlink(missing_ok=True)
            raise


def _remove_stale_temps(capture: Path) -> None:
    """Unlink this relay's own temp records older than :data:`_STALE_TEMP_SECONDS` (never a
    symlink's target: ``unlink`` removes the link). Bounded and silent."""
    cutoff = time.time() - _STALE_TEMP_SECONDS
    with contextlib.suppress(OSError), os.scandir(capture) as entries:
        for scanned, entry in enumerate(entries):
            if scanned >= _MAX_TEMP_SCAN:
                break
            name = entry.name
            if not (name.startswith(_TEMP_PREFIX) and name.endswith(_TEMP_SUFFIX)):
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)  # noqa: PTH108 (a DirEntry path, no Path needed)


def read_last_outcome(data_dir: Path) -> dict[str, object] | None:
    """The relay's last recorded outcome (``at``, ``outcome``, ``http_status``), or
    ``None`` when there is none or it is not one the relay would write."""
    try:
        record = json.loads((data_dir / "capture" / LAST_OUTCOME_FILENAME).read_text("ascii"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or set(record) != {"at", "outcome", "http_status"}:
        return None
    at, code, status = record["at"], record["outcome"], record["http_status"]
    if not isinstance(at, str) or len(at) > 40 or code not in OUTCOMES:
        return None
    if status is not None and (not isinstance(status, int) or isinstance(status, bool)):
        return None
    return {"at": at, "outcome": code, "http_status": status}


def _options(argv: Sequence[str]) -> dict[str, str]:
    """``--data-dir`` and ``--url`` from ``argv``; anything else is ignored."""
    options: dict[str, str] = {}
    args = iter(argv)
    for arg in args:
        if arg in ("--data-dir", "--url"):
            options[arg] = next(args, "")
    return options


def main(argv: Sequence[str]) -> None:
    """The command hook: relay stdin, then leave with ``os._exit(0)`` whatever happened
    (nothing flushed, no interpreter shutdown to wait through, no daemon thread joined)."""
    with contextlib.suppress(BaseException):
        options = _options(argv)
        data_dir = Path(options.get("--data-dir", ""))
        if data_dir.is_absolute():
            outcome = relay(
                url=options.get("--url", ""), data_dir=data_dir, stdin_fd=0, environ=os.environ
            )
            record_outcome(data_dir, outcome)
    os._exit(0)


def probe() -> None:
    """Prove this interpreter can run the relay: import everything the relay imports (its
    standard library modules already are, by now) and say so on stdout. Never posts or
    reads anything."""
    try:
        import importlib

        importlib.import_module("agenttape.security.client_token")
    except BaseException as exc:
        os.write(2, f"agenttape-hook-relay: {type(exc).__name__}: {exc}\n".encode())
        os._exit(1)
    os.write(1, PROBE_OUTPUT)
    os._exit(0)


if __name__ == "__main__":
    if sys.argv[1:2] == [PROBE_ARG]:
        probe()
    main(sys.argv[1:])
