import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from zimantic.__main__ import main
from zimantic.diagnose import diagnose_archive


def test_native_abort_is_named_and_isolated():
    completed = subprocess.CompletedProcess([], -signal.SIGABRT, "", "Cannot find entry")
    with patch("zimantic.diagnose.subprocess.run", return_value=completed) as run:
        report = diagnose_archive(Path("/tmp/faulty.zim"), queries=["hawaii"])
    assert report["status"] == "crash"
    assert report["name"] == "faulty.zim"
    assert "SIGABRT" in report["error"]
    assert run.call_args.args[0][-2:] == ["-m", "zimantic.diagnose"]
    assert json.loads(run.call_args.kwargs["input"])["queries"] == ["hawaii"]


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="POSIX resource kill")
def test_killed_helper_does_not_claim_archive_is_corrupt():
    completed = subprocess.CompletedProcess([], -signal.SIGKILL, "", "")
    with patch("zimantic.diagnose.subprocess.run", return_value=completed):
        report = diagnose_archive(Path("/tmp/manual.zim"))
    assert report["status"] == "killed"
    assert "archive not validated" in report["error"]


def test_timeout_preserves_last_native_operation():
    timeout = subprocess.TimeoutExpired([], 0.01, stderr=b"manual.zim: checksum\n")
    with patch("zimantic.diagnose.subprocess.run", side_effect=timeout):
        report = diagnose_archive(Path("/tmp/manual.zim"), timeout=0.01)
    assert report["status"] == "timeout"
    assert report["diagnostics"] == "manual.zim: checksum\n"


def test_nonzero_helper_exit_cannot_report_a_pass():
    completed = subprocess.CompletedProcess([], 1, '{"status": "ok"}', "shutdown failed")
    with patch("zimantic.diagnose.subprocess.run", return_value=completed):
        report = diagnose_archive(Path("/tmp/manual.zim"))
    assert report["status"] == "error"
    assert report["diagnostics"] == "shutdown failed"


def test_cli_continues_after_failure_without_loading_config(tmp_path, capsys):
    first, second = tmp_path / "a.zim", tmp_path / "b.zim"
    first.write_bytes(b"not opened in parent")
    second.write_bytes(b"not opened in parent")
    reports = [
        {"name": first.name, "status": "crash", "error": "SIGABRT"},
        {"name": second.name, "status": "ok", "has_fulltext_index": False,
         "checksum_checked": False, "searches": []},
    ]
    with (
        patch("zimantic.settings.load_config", side_effect=AssertionError("config loaded")),
        patch("zimantic.diagnose.diagnose_archive", side_effect=reports) as check,
        pytest.raises(SystemExit) as exit,
    ):
        main(["diagnose", str(tmp_path), "--query", "hawaii", "--json"])
    assert exit.value.code == 1
    assert check.call_count == 2
    assert [report["status"] for report in json.loads(capsys.readouterr().out)["archives"]] == ["crash", "ok"]


@pytest.mark.parametrize("option,value", [
    ("--timeout", "0"), ("--timeout", "nan"), ("--limit", "0"), ("--query", ""),
])
def test_cli_rejects_invalid_probe_options(option, value):
    with pytest.raises(SystemExit) as exit:
        main(["diagnose", "manual.zim", option, value])
    assert exit.value.code == 2
