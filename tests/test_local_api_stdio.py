"""stdio transport for policy-declared local JSON-RPC APIs."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from sentinelx_core.local_api import LocalApiError, call_action
from sentinelx_core.policy import Policy


@pytest.fixture(autouse=True)
def _clear_compatibility_cache():
    from sentinelx_core import local_api

    local_api._compat_verdicts.clear()
    yield
    local_api._compat_verdicts.clear()


def _script(tmp_path: Path, body: str, *, name: str = "endpoint.py") -> Path:
    path = tmp_path / name
    path.write_text(
        f"#!{sys.executable}\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _policy(
    tmp_path: Path,
    executable: Path,
    *,
    actions: str = "echo: { method: test.echo }",
    timeout_s: float = 2.0,
    compatibility: str = "",
    protocol: str = "jsonrpc",
    run_as: str = "",
) -> Policy:
    config = tmp_path / "config.yaml"
    compat_block = (
        textwrap.indent(textwrap.dedent(compatibility).strip(), "    ") + "\n"
        if compatibility
        else ""
    )
    run_as_line = f"    run_as: {run_as}\n" if run_as else ""
    config.write_text(
        (
            "local_apis:\n"
            "  test:\n"
            "    transport: stdio\n"
            f"    path: {executable}\n"
            f"    protocol: {protocol}\n"
            f"    timeout_s: {timeout_s}\n"
            f"{run_as_line}"
            f"{compat_block}"
            "    actions:\n"
            + textwrap.indent(textwrap.dedent(actions).strip(), "      ")
            + "\n"
        ),
        encoding="utf-8",
    )
    return Policy.from_file(config)


async def test_stdio_jsonrpc_positive_call_and_params_stay_stdin_data(
    tmp_path: Path,
) -> None:
    side_effect = tmp_path / "must-not-exist"
    endpoint = _script(
        tmp_path,
        """
        import json
        import os
        import sys

        request = json.loads(sys.stdin.readline())
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "argv": sys.argv,
                "params": request["params"],
                "env_keys": sorted(os.environ),
            },
        }, separators=(",", ":")))
        """,
    )
    policy = _policy(tmp_path, endpoint)
    value = f'$(touch {side_effect}) ; echo injected'
    result = await call_action(policy.local_apis["test"], "echo", {"value": value})

    assert result["argv"] == [str(endpoint)]
    assert result["params"] == {"value": value}
    assert not side_effect.exists()
    assert set(result["env_keys"]) <= {"PATH", "LANG", "LC_ALL", "SystemRoot", "WINDIR"}


async def test_stdio_child_environment_does_not_inherit_agent_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTINELX_SECRET_TEST", "must-not-inherit")
    endpoint = _script(
        tmp_path,
        """
        import json
        import os
        import sys

        request = json.loads(sys.stdin.readline())
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": dict(os.environ),
        }))
        """,
    )
    policy = _policy(tmp_path, endpoint)
    result = await call_action(policy.local_apis["test"], "echo", {})

    assert "SENTINELX_SECRET_TEST" not in result
    assert result["PATH"]
    assert result["LANG"] == "C.UTF-8"
    assert result["LC_ALL"] == "C.UTF-8"


async def test_stdio_action_allowlist_blocks_unknown_action_before_spawn(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "spawned"
    endpoint = _script(
        tmp_path,
        f"""
        from pathlib import Path
        Path({str(marker)!r}).write_text("spawned")
        """,
    )
    policy = _policy(tmp_path, endpoint)

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "not-allowed", {})
    assert exc.value.code == "action_not_allowed"
    assert not marker.exists()


async def test_stdio_timeout_is_structured_and_child_is_stopped(tmp_path: Path) -> None:
    endpoint = _script(
        tmp_path,
        """
        import time
        time.sleep(5)
        """,
    )
    policy = _policy(tmp_path, endpoint, timeout_s=0.05)

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "timeout"


async def test_stdio_stdout_is_bounded(tmp_path: Path) -> None:
    endpoint = _script(
        tmp_path,
        """
        import sys
        sys.stdout.write("x" * (1024 * 1024 + 100))
        sys.stdout.flush()
        """,
    )
    policy = _policy(tmp_path, endpoint)

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "too_large"
    assert "stdout" in exc.value.message


async def test_stdio_stderr_is_bounded_and_never_echoed(tmp_path: Path) -> None:
    endpoint = _script(
        tmp_path,
        """
        import sys
        sys.stderr.write("PRIVATE-DIAGNOSTIC-" + "x" * (20 * 1024))
        sys.stderr.flush()
        """,
    )
    policy = _policy(tmp_path, endpoint)

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "too_large"
    assert "PRIVATE-DIAGNOSTIC" not in exc.value.message


async def test_stdio_nonzero_exit_reports_only_sanitized_stderr_class(
    tmp_path: Path,
) -> None:
    endpoint = _script(
        tmp_path,
        """
        import sys
        sys.stderr.write("DATABASE_URL=do-not-leak")
        raise SystemExit(7)
        """,
    )
    policy = _policy(tmp_path, endpoint)

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "endpoint_process_failed"
    assert "rc=7" in exc.value.message
    assert "stderr=nonempty" in exc.value.message
    assert "DATABASE_URL" not in exc.value.message
    assert "do-not-leak" not in exc.value.message


async def test_stdio_malformed_or_multiple_output_fails_closed(tmp_path: Path) -> None:
    malformed = _script(
        tmp_path,
        'print("not-json")\n',
        name="malformed.py",
    )
    policy = _policy(tmp_path, malformed)
    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "bad_response"

    multiple = _script(
        tmp_path,
        'print("{}")\nprint("{}")\n',
        name="multiple.py",
    )
    policy = _policy(tmp_path, multiple)
    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "bad_response"
    assert "exactly one" in exc.value.message


async def test_stdio_spawn_failure_is_structured(tmp_path: Path) -> None:
    policy = _policy(tmp_path, Path("/definitely/not/a/real/stdio-endpoint"))

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "endpoint_unreachable"


async def test_stdio_compatibility_probe_uses_same_transport(tmp_path: Path) -> None:
    marker = tmp_path / "probe-ran"
    endpoint = _script(
        tmp_path,
        f"""
        import json
        from pathlib import Path
        import sys

        request = json.loads(sys.stdin.readline())
        if request["method"] == "identity.describe":
            Path({str(marker)!r}).write_text("yes")
            result = {{"api_version": "1"}}
        else:
            result = {{"probe_seen": Path({str(marker)!r}).exists()}}
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": result,
        }}))
        """,
    )
    policy = _policy(
        tmp_path,
        endpoint,
        compatibility="""
        compatibility:
          probe: { method: identity.describe }
          extract: api_version
          accept: { exact: "1" }
        """,
    )
    result = await call_action(policy.local_apis["test"], "echo", {})

    assert marker.exists()
    assert result == {"probe_seen": True}


async def test_stdio_compatibility_is_reprobed_for_each_process_epoch(
    tmp_path: Path,
) -> None:
    counter = tmp_path / "probe-count"
    endpoint = _script(
        tmp_path,
        f"""
        import json
        from pathlib import Path
        import sys

        request = json.loads(sys.stdin.readline())
        if request["method"] == "identity.describe":
            path = Path({str(counter)!r})
            count = int(path.read_text()) if path.exists() else 0
            path.write_text(str(count + 1))
            result = {{"api_version": "1"}}
        else:
            result = {{"ok": True}}
        print(json.dumps({{
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": result,
        }}))
        """,
    )
    policy = _policy(
        tmp_path,
        endpoint,
        compatibility="""
        compatibility:
          probe: { method: identity.describe }
          extract: api_version
          accept: { exact: "1" }
        """,
    )

    await call_action(policy.local_apis["test"], "echo", {})
    await call_action(policy.local_apis["test"], "echo", {})

    assert counter.read_text() == "2"


async def test_stdio_compatibility_mismatch_fails_closed(tmp_path: Path) -> None:
    endpoint = _script(
        tmp_path,
        """
        import json
        import sys

        request = json.loads(sys.stdin.readline())
        print(json.dumps({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"api_version": "2"},
        }))
        """,
    )
    policy = _policy(
        tmp_path,
        endpoint,
        compatibility="""
        compatibility:
          probe: { method: identity.describe }
          extract: api_version
          accept: { exact: "1" }
        """,
    )

    with pytest.raises(LocalApiError) as exc:
        await call_action(policy.local_apis["test"], "echo", {})
    assert exc.value.code == "compatibility_mismatch"


def test_stdio_rejects_http_relative_path_and_run_as(tmp_path: Path) -> None:
    endpoint = _script(tmp_path, "pass\n")

    http = _policy(tmp_path, endpoint, protocol="http", actions='x: { request: "GET /" }')
    assert http.local_apis == {}

    relative = _policy(tmp_path, Path("relative-endpoint"))
    assert relative.local_apis == {}

    run_as = _policy(tmp_path, endpoint, run_as="someone")
    assert run_as.local_apis == {}


def test_stdio_http_style_compatibility_probe_is_dropped(tmp_path: Path) -> None:
    endpoint = _script(tmp_path, "pass\n")
    policy = _policy(
        tmp_path,
        endpoint,
        compatibility="""
        compatibility:
          probe: { request: "GET /version" }
          extract: version
          accept: { exact: 1 }
        """,
    )
    assert policy.local_apis["test"].compatibility == {}
