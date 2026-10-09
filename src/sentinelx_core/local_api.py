"""Talk to a host-local endpoint that already speaks a structured protocol.

WHY THIS EXISTS. To read something a local tool already knows, the assistant
goes through `exec`: build a command line, spawn a process, and parse output
written for human eyes. Docker is the clearest case, at 568 distinct users in a
week, and its most-run subcommands are READS that /var/run/docker.sock already
answers in JSON.

WHAT IT KNOWS. Open a socket, speak HTTP or JSON-RPC, make the declared request,
keep the declared fields. Nothing about Docker or any other product lives here;
that knowledge is in the host's YAML. Same shape as `services:`, where the agent
holds a generic systemctl mechanism and no nginx profile in code.

PROJECTION IS NOT COSMETIC. Measured against nine real containers: `docker ps`
text 712 bytes, raw socket JSON for the same question 20,336, curated projection
1,457. Passing the raw body through would be 28x worse than the text it replaces,
so every action declares what to keep.

NO SHELL. With transport "unix" there is no process, no argv and no shell, so
the entire shell-injection class simply cannot occur on this path. In the audit,
"command_not_allowed: contains shell operator" is among the most frequent
failures on the exec path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any
from uuid import uuid4

from sentinelx_core.winspawn import spawn_kwargs

logger = logging.getLogger(__name__)

# A body larger than this is refused rather than buffered. The far side is
# opaque to us: it can answer with anything, and a container log endpoint can
# stream indefinitely.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_STDIO_RESPONSE_BYTES = 1024 * 1024
MAX_STDIO_STDERR_BYTES = 16 * 1024


class LocalApiError(Exception):
    """Something went wrong talking to a local endpoint."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def project(data: Any, select: tuple[str, ...]) -> Any:
    """Keep only `select` from a dict, or from every dict in a list.

    Supports dotted paths ("Config.Image") because the interesting field is
    often nested. An empty `select` returns the data untouched, which is the
    escape hatch for endpoints whose answers are already small.
    """
    if not select:
        return data
    if isinstance(data, list):
        return [project(item, select) for item in data]
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for spec in select:
        cur: Any = data
        for part in spec.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur is not None:
            out[spec] = cur
    return out


def _render(template: str, params: dict[str, Any]) -> str:
    """Substitute {name} placeholders from `params`.

    Values are quoted for a URL path, not for a shell: there is no shell here.
    A missing placeholder is an error rather than an empty string, because
    silently requesting /containers//json is worse than refusing.
    """
    from urllib.parse import quote

    out = template
    for key, value in (params or {}).items():
        out = out.replace("{" + str(key) + "}", quote(str(value), safe=""))
    if "{" in out and "}" in out:
        missing = out[out.index("{") + 1 : out.index("}")]
        raise LocalApiError(
            "missing_param", f"the action needs a value for '{missing}'"
        )
    return out


async def _read_http_response(reader: asyncio.StreamReader) -> bytes:
    """Read one HTTP/1.1 response body. Handles both framings Docker uses."""
    header_blob = await reader.readuntil(b"\r\n\r\n")
    head = header_blob.decode("latin-1")
    status_line = head.split("\r\n", 1)[0]
    try:
        status = int(status_line.split(" ")[1])
    except (IndexError, ValueError) as exc:
        raise LocalApiError("bad_response", f"unparseable status: {status_line!r}") from exc

    headers = {}
    for line in head.split("\r\n")[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks = bytearray()
        while True:
            size_line = (await reader.readuntil(b"\r\n")).strip()
            size = int(size_line.split(b";")[0] or b"0", 16)
            if size == 0:
                await reader.readuntil(b"\r\n")
                break
            chunks += await reader.readexactly(size)
            await reader.readexactly(2)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise LocalApiError("too_large", "endpoint response exceeded the cap")
        body = bytes(chunks)
    else:
        length = int(headers.get("content-length") or 0)
        if length > MAX_RESPONSE_BYTES:
            raise LocalApiError("too_large", "endpoint response exceeded the cap")
        body = await reader.readexactly(length) if length else b""

    if status >= 400:
        raise LocalApiError(
            "endpoint_error",
            f"endpoint answered HTTP {status}: {body[:200].decode('utf-8', 'replace')}",
        )
    return body


async def call_http(endpoint: Any, action: Any, params: dict[str, Any]) -> Any:
    """One HTTP request over a unix socket. Docker's shape."""
    raw = (action.request or "").strip()
    try:
        method, target = raw.split(" ", 1)
    except ValueError as exc:
        raise LocalApiError(
            "bad_action", f"request must look like 'GET /path', got {raw!r}"
        ) from exc
    target = _render(target.strip(), params)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(endpoint.path), timeout=endpoint.timeout_s
        )
    except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
        raise LocalApiError(
            "endpoint_unreachable",
            f"cannot open {endpoint.path}: {exc.__class__.__name__}",
        ) from exc
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise LocalApiError("timeout", f"timed out opening {endpoint.path}") from exc

    try:
        request = (
            f"{method.upper()} {target} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(request.encode("latin-1"))
        await writer.drain()
        body = await asyncio.wait_for(
            _read_http_response(reader), timeout=endpoint.timeout_s
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise LocalApiError("timeout", f"{endpoint.name} did not answer in time") from exc
    except asyncio.IncompleteReadError as exc:
        raise LocalApiError("bad_response", "endpoint closed mid-response") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - closing must not mask the real error
            pass

    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError as exc:
        raise LocalApiError("bad_response", "endpoint did not return JSON") from exc


# ---------------------------------------------------------------------------
# run_as: reach a socket owned by a different Unix user
#
# An endpoint like Herdr creates its socket 0600 and checks the peer
# credentials, so reaching it means BEING that user. Opening it as the agent's
# own user fails with EACCES, and running the agent as root does not help
# either: root connects but identifies as uid 0, which is not the owner.
#
# So when `run_as` is declared, the connect happens in a tiny helper invoked
# through `sudo -n -u <user>`. Measured on a real 0600 socket: as the agent
# user EACCES, as root peer_uid=0, through the helper peer_uid=<target>.
#
# WHY NOT CAP_SETUID. Forking and setuid'ing before connect also works and
# needs no sudo, but it requires AmbientCapabilities=CAP_SETUID on the unit:
# a permanently privileged agent on every host in the fleet to serve a feature
# few will use. Sudo keeps the cost on the hosts that opt in.
#
# THE OPERATOR HAS TO ALLOW IT. Since the OpenAI security work, sudo is not
# granted by default, so `run_as` does not silently acquire authority: it fails
# with an explanation naming the exact sudoers line, and the host's owner
# decides. That is the same shape the requester asked for, which was not to
# weaken the socket's permissions nor put the agent in a shared group.
# ---------------------------------------------------------------------------


def _relay_command(endpoint: Any) -> list[str]:
    return [
        "sudo", "-n", "-u", endpoint.run_as,
        sys.executable, "-m", "sentinelx_core.local_api_relay",
        endpoint.path, str(endpoint.timeout_s),
    ]


async def _call_via_run_as(endpoint: Any, payload: bytes) -> bytes:
    """Send one request through the relay, under the declared Unix identity."""
    proc = await asyncio.create_subprocess_exec(
        *_relay_command(endpoint),
        **spawn_kwargs(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(payload), timeout=endpoint.timeout_s + 5
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        proc.kill()
        raise LocalApiError(
            "timeout", f"{endpoint.name} did not answer in time"
        ) from exc

    if proc.returncode == 0 and out:
        return out

    detail = (err or b"").decode("utf-8", "replace").strip()
    # sudo's own refusal is the common case and deserves a real answer rather
    # than the raw message, because the fix is a specific sudoers line.
    if "a password is required" in detail or "not allowed to execute" in detail or (
        proc.returncode == 1 and "sudo" in detail
    ):
        raise LocalApiError(
            "run_as_not_permitted",
            f"'{endpoint.name}' declares run_as={endpoint.run_as!r}, but this "
            f"agent may not become that user. The host's owner can allow just "
            f"this, and nothing else, with a sudoers rule such as:\n"
            f"  sentinelx ALL=({endpoint.run_as}) NOPASSWD: "
            f"{sys.executable} -m sentinelx_core.local_api_relay\n"
            f"Until then this endpoint is unreachable. ({detail[:120]})",
        )
    if proc.returncode == 3:
        raise LocalApiError(
            "endpoint_unreachable",
            f"cannot open {endpoint.path} as {endpoint.run_as}: {detail[:160]}",
        )
    raise LocalApiError(
        "bad_response", f"relay failed (rc={proc.returncode}): {detail[:160]}"
    )


def _stdio_env() -> dict[str, str]:
    """Minimal deterministic child environment for local-api stdio endpoints."""
    env = {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if sys.platform == "win32":
        for key in ("SystemRoot", "WINDIR"):
            value = os.environ.get(key)
            if value:
                env[key] = value
    return env


async def _read_bounded_stream(
    reader: asyncio.StreamReader | None,
    *,
    limit: int,
    label: str,
) -> bytes:
    if reader is None:
        return b""
    data = bytearray()
    while True:
        remaining = limit + 1 - len(data)
        chunk = await reader.read(min(65536, max(1, remaining)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise LocalApiError(
                "too_large",
                f"{label} exceeded the {limit}-byte cap",
            )


async def _kill_and_wait(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
    try:
        await proc.wait()
    except ProcessLookupError:
        pass


async def _call_jsonrpc_stdio(endpoint: Any, payload: dict[str, Any]) -> Any:
    """One bounded JSON-RPC exchange with a fixed executable over stdin/stdout."""
    if endpoint.protocol != "jsonrpc":
        raise LocalApiError(
            "unsupported_transport_protocol",
            f"stdio transport does not support protocol {endpoint.protocol!r}",
        )
    if getattr(endpoint, "run_as", None):
        raise LocalApiError(
            "unsupported_run_as",
            "stdio local-api endpoints do not support run_as",
        )
    executable = str(endpoint.path)
    if not os.path.isabs(executable):
        raise LocalApiError(
            "invalid_endpoint",
            "stdio local-api executable path must be absolute",
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            cwd=os.path.abspath(os.sep),
            env=_stdio_env(),
            **spawn_kwargs(
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise LocalApiError(
            "endpoint_unreachable",
            f"cannot spawn configured stdio endpoint: {exc.__class__.__name__}",
        ) from exc

    request = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
    try:
        if proc.stdin is None:
            raise LocalApiError("bad_response", "stdio endpoint stdin unavailable")
        proc.stdin.write(request)
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            # The child may have exited immediately; the structured exit/result
            # handling below classifies that without exposing raw diagnostics.
            pass
        proc.stdin.close()

        stdout_task = asyncio.create_task(
            _read_bounded_stream(
                proc.stdout,
                limit=MAX_STDIO_RESPONSE_BYTES,
                label="stdio endpoint stdout",
            )
        )
        stderr_task = asyncio.create_task(
            _read_bounded_stream(
                proc.stderr,
                limit=MAX_STDIO_STDERR_BYTES,
                label="stdio endpoint stderr",
            )
        )
        wait_task = asyncio.create_task(proc.wait())
        tasks = (stdout_task, stderr_task, wait_task)
        try:
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=endpoint.timeout_s,
            )
        except TimeoutError as exc:
            await _kill_and_wait(proc)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise LocalApiError(
                "timeout",
                f"{endpoint.name} stdio endpoint did not answer in time",
            ) from exc
        except LocalApiError:
            await _kill_and_wait(proc)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()

    if proc.returncode != 0:
        stderr_class = "nonempty" if stderr else "empty"
        raise LocalApiError(
            "endpoint_process_failed",
            f"stdio endpoint exited rc={proc.returncode}; stderr={stderr_class}",
        )

    if not stdout:
        raise LocalApiError("bad_response", "stdio endpoint returned no JSON response")

    lines = stdout.splitlines()
    nonempty = [line for line in lines if line.strip()]
    if len(nonempty) != 1:
        raise LocalApiError(
            "bad_response",
            "stdio endpoint must return exactly one JSON response line",
        )
    try:
        message = json.loads(nonempty[0])
    except ValueError as exc:
        raise LocalApiError(
            "bad_response",
            "stdio endpoint did not return valid JSON",
        ) from exc
    if not isinstance(message, dict):
        raise LocalApiError(
            "bad_response",
            "stdio endpoint JSON response must be an object",
        )
    if message.get("error"):
        err = message["error"]
        raise LocalApiError(
            "endpoint_error",
            (
                f"{err.get('code')}: {err.get('message')}"
                if isinstance(err, dict)
                else "endpoint returned a JSON-RPC error"
            ),
        )
    return message.get("result")


async def call_jsonrpc(endpoint: Any, action: Any, params: dict[str, Any]) -> Any:
    """One JSON-RPC 2.0 call over the endpoint's declared transport."""
    payload = {
        "jsonrpc": "2.0",
        # A STRING id. JSON-RPC 2.0 permits either, but a receiver may declare
        # it as a string in its own schema, and a string is the shape that
        # satisfies both. Named after the caller so it is recognisable in an
        # endpoint's logs.
        "id": f"sentinel-local-api-{uuid4().hex[:8]}",
        "method": action.method,
        "params": params or {},
    }
    transport = str(getattr(endpoint, "transport", "unix") or "unix")
    if transport == "stdio":
        return await _call_jsonrpc_stdio(endpoint, payload)
    if transport != "unix":
        raise LocalApiError(
            "unsupported_transport_protocol",
            f"unsupported local-api transport {transport!r} for jsonrpc",
        )

    if getattr(endpoint, "run_as", None):
        line = await _call_via_run_as(
            endpoint, (json.dumps(payload) + "\n").encode()
        )
        try:
            message = json.loads(line)
        except ValueError as exc:
            raise LocalApiError(
                "bad_response", "endpoint did not return JSON"
            ) from exc
        if isinstance(message, dict) and message.get("error"):
            err = message["error"]
            raise LocalApiError(
                "endpoint_error",
                f"{err.get('code')}: {err.get('message')}"
                if isinstance(err, dict) else str(err),
            )
        return message.get("result") if isinstance(message, dict) else message
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(endpoint.path), timeout=endpoint.timeout_s
        )
    except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
        raise LocalApiError(
            "endpoint_unreachable",
            f"cannot open {endpoint.path}: {exc.__class__.__name__}",
        ) from exc
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise LocalApiError("timeout", f"timed out opening {endpoint.path}") from exc

    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=endpoint.timeout_s)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise LocalApiError("timeout", f"{endpoint.name} did not answer in time") from exc
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    if not line:
        raise LocalApiError("bad_response", "endpoint closed without answering")
    try:
        message = json.loads(line)
    except ValueError as exc:
        raise LocalApiError("bad_response", "endpoint did not return JSON") from exc
    if isinstance(message, dict) and message.get("error"):
        err = message["error"]
        raise LocalApiError(
            "endpoint_error",
            f"{err.get('code')}: {err.get('message')}" if isinstance(err, dict) else str(err),
        )
    return message.get("result") if isinstance(message, dict) else message


# ---------------------------------------------------------------------------
# Compatibility, per connection epoch (core#45)
#
# THE CONTRACT, in the requester's own words: "the profile declares how to
# obtain compatibility metadata and which values it accepts; SentinelX
# evaluates that declared constraint."
#
# WHY NOT PER CALL. A probe before every call pays a round trip for a guarantee
# it does not deliver: the endpoint can restart between the check and the call
# regardless. Binding validation to a connection epoch gives a real boundary
# instead of an expensive approximation.
#
# WHY NOT AT REGISTRATION. Probing when the agent boots would mean an endpoint
# that happens to be down at that moment never registers at all, and stays dead
# until someone restarts the agent. Validation is therefore lazy: first use in
# an epoch checks, the verdict is cached, and the cache entry is dropped the
# moment the endpoint becomes unreachable, because that IS the epoch ending.
#
# WHY NEVER `>=`. A higher version number does not imply the protocol still
# matches. `exact` is the default and `allowed` is how a maintainer widens it
# on purpose, which puts the judgement with whoever actually knows the answer.
# ---------------------------------------------------------------------------

# endpoint name -> True (passed) | (code, message) (failed). Absent means "not
# checked in this epoch".
_compat_verdicts: dict[str, Any] = {}


def forget_compatibility(endpoint_name: str) -> None:
    """Drop a cached verdict. Called when an endpoint goes unreachable."""
    _compat_verdicts.pop(endpoint_name, None)


def _extract(data: Any, path: str) -> Any:
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


async def ensure_compatible(endpoint: Any) -> None:
    """Check the declared constraint once per epoch. Raises on mismatch.

    Fails CLOSED: an endpoint that cannot be probed, or whose version cannot be
    read, is refused rather than allowed through on the assumption it is fine.
    """
    constraint = getattr(endpoint, "compatibility", None) or {}
    if not constraint:
        return

    cached = _compat_verdicts.get(endpoint.name)
    if cached is True:
        return
    if isinstance(cached, tuple):
        raise LocalApiError(cached[0], cached[1])

    probe = constraint["probe"]
    accept = constraint["accept"]
    field = constraint["extract"]

    # The probe is described exactly like an action, so it goes through the same
    # transport rather than a parallel code path that could drift from it.
    probe_action = type(
        "ProbeAction",
        (),
        {
            "request": probe.get("request"),
            "method": probe.get("method"),
            "select": (),
        },
    )()
    try:
        if endpoint.protocol == "http":
            reply = await call_http(endpoint, probe_action, {})
        else:
            reply = await call_jsonrpc(endpoint, probe_action, {})
    except LocalApiError as exc:
        if exc.code in ("endpoint_unreachable", "timeout", "run_as_not_permitted"):
            # Not a verdict: the epoch never started. Leave the cache empty so
            # the next attempt probes again instead of inheriting a failure
            # that was only ever about reachability.
            #
            # run_as_not_permitted belongs here too, and for a sharper reason:
            # wrapping it as compatibility_unknown tells the operator their
            # endpoint has a version problem when what they actually need is a
            # sudoers line. The probe runs first, so without this the real cause
            # is hidden behind the first thing that happened to fail.
            raise
        verdict = (
            "compatibility_unknown",
            f"could not read the compatibility metadata '{field}' from "
            f"'{endpoint.name}': {exc.message}",
        )
        _compat_verdicts[endpoint.name] = verdict
        raise LocalApiError(*verdict) from exc

    found = _extract(reply, field)
    if found is None:
        verdict = (
            "compatibility_unknown",
            f"'{endpoint.name}' did not report '{field}', so its compatibility "
            "cannot be established. Refusing rather than assuming.",
        )
        _compat_verdicts[endpoint.name] = verdict
        raise LocalApiError(*verdict)

    if "exact" in accept:
        ok = found == accept["exact"]
        wanted = f"exactly {accept['exact']!r}"
    else:
        ok = found in (accept.get("allowed") or [])
        wanted = f"one of {accept.get('allowed')!r}"

    if not ok:
        verdict = (
            "compatibility_mismatch",
            f"'{endpoint.name}' reports {field}={found!r}, but this profile "
            f"accepts {wanted}. Widen `compatibility.accept.allowed` if the "
            "profile's maintainer says the versions are compatible; SentinelX "
            "will not assume a newer version is.",
        )
        _compat_verdicts[endpoint.name] = verdict
        logger.warning(
            "local_api compatibility mismatch on %s: %s=%r", endpoint.name, field, found
        )
        raise LocalApiError(*verdict)

    _compat_verdicts[endpoint.name] = True
    logger.info(
        "local_api compatibility ok on %s: %s=%r", endpoint.name, field, found
    )


async def call_action(endpoint: Any, action_name: str, params: dict[str, Any]) -> Any:
    """Run one ALLOWLISTED action and return its projected result."""
    action = endpoint.actions.get(action_name)
    if action is None:
        # Naming the permitted actions is deliberate: they are already visible
        # through `describe`, and guessing is the failure this avoids.
        raise LocalApiError(
            "action_not_allowed",
            f"'{action_name}' is not an allowed action on '{endpoint.name}'. "
            f"Allowed: {sorted(endpoint.actions)}",
        )
    await ensure_compatible(endpoint)
    try:
        if endpoint.protocol == "http":
            raw = await call_http(endpoint, action, params)
        else:
            raw = await call_jsonrpc(endpoint, action, params)
    except LocalApiError as exc:
        if exc.code in ("endpoint_unreachable", "timeout"):
            # The epoch ended. Whatever we concluded about this endpoint no
            # longer describes whatever comes back, so re-check next time.
            forget_compatibility(endpoint.name)
        raise
    return project(raw, action.select)
