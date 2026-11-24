"""T19 (the parts that need no server): the deploy files say what Step 19 requires."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from apps.platform.alerts import RecordingAlerts
from apps.platform.disk import DiskReading, check_disk
from apps.platform.models import AlertState

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
ROOT = DEPLOY.parent

needs_deploy = pytest.mark.skipif(not DEPLOY.exists(), reason="deploy/ is not in this image")


def text(*parts: str) -> str:
    return DEPLOY.joinpath(*parts).read_text()


@needs_deploy
def test_the_batch_timer_fires_at_six_eastern_on_weekdays() -> None:
    timer = text("systemd", "oxhouse-batch.timer")
    assert "OnCalendar=Mon..Fri 06:00:00 America/New_York" in timer
    assert "run_batch --schedule" in text("systemd", "oxhouse-batch.service")


@needs_deploy
def test_the_backup_timer_runs_nightly_and_keeps_thirty_days() -> None:
    assert "OnCalendar=*-*-* 02:00:00" in text("systemd", "oxhouse-backup.timer")
    script = text("backup.sh")
    assert "BACKUP_KEEP_DAYS:-30" in script
    assert "pg_dump" in script and "-Fc" in script


@needs_deploy
def test_only_the_proxy_publishes_ports_and_the_database_never_does() -> None:
    compose = text("docker-compose.prod.yml")
    published: dict[str, bool] = {}
    current = ""
    in_services = False
    for line in compose.splitlines():
        if line.startswith("services:"):
            in_services = True
        elif line and not line.startswith((" ", "#")):
            in_services = False
        match = re.match(r"^  ([a-z_]+):\s*$", line)
        if in_services and match:
            current = match.group(1)
            published[current] = False
        elif in_services and current and line.startswith("    ports:"):
            published[current] = True
    assert published["caddy"] is True
    assert published["db"] is False and published["app"] is False
    assert 'DJANGO_DEBUG: "false"' in compose


@needs_deploy
def test_the_proxy_allows_only_listed_networks() -> None:
    caddy = text("Caddyfile")
    assert "remote_ip {$ALLOWED_NETWORKS}" in caddy
    assert 'respond "Forbidden" 403' in caddy
    assert "{$SITE_DOMAIN}" in caddy  # HTTPS comes from naming a domain


@needs_deploy
def test_no_secret_is_committed() -> None:
    assert not list(DEPLOY.glob(".env*"))
    example = text("env.production.example")
    for line in example.splitlines():
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            if any(w in name for w in ("PASSWORD", "SECRET", "TOKEN", "KEY", "DSN")):
                assert value == "", f"{name} has a value in the example"
    ignore = (ROOT / ".gitignore").read_text()
    assert ".env" in ignore and ".env.*" in ignore
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text()
    assert "secrets.DEPLOY_SSH_KEY" in workflow


@needs_deploy
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
@pytest.mark.parametrize("script", ["backup.sh", "restore.sh", "deploy.sh"])
def test_the_shell_scripts_parse(script: str) -> None:
    result = subprocess.run(["bash", "-n", str(DEPLOY / script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# --- the disk check (monitoring) --------------------------------------------------------------


def reader(percent: float) -> Any:
    return lambda path: DiskReading(path, percent)


@pytest.mark.django_db
def test_disk_above_eighty_percent_alerts_once_and_again_after_recovery() -> None:
    sink = RecordingAlerts()
    check_disk(["/data"], sink=sink, reader=reader(81.0))
    check_disk(["/data"], sink=sink, reader=reader(90.0))
    assert sink.kinds() == ["disk_usage"]
    check_disk(["/data"], sink=sink, reader=reader(60.0))
    assert AlertState.objects.get(key="disk_usage:/data").active is False
    check_disk(["/data"], sink=sink, reader=reader(85.0))
    assert sink.kinds() == ["disk_usage", "disk_usage"]


@pytest.mark.django_db
def test_disk_at_or_below_the_limit_is_quiet() -> None:
    sink = RecordingAlerts()
    check_disk(["/data"], sink=sink, reader=reader(80.0))
    assert sink.sent == []


@pytest.mark.django_db
def test_each_path_is_its_own_condition() -> None:
    sink = RecordingAlerts()
    readings = check_disk(
        ["/a", "/b"],
        sink=sink,
        reader=lambda p: DiskReading(p, 95.0 if p == "/a" else 10.0),
    )
    assert [r.path for r in readings] == ["/a", "/b"]
    assert [s[1] for s in sink.sent] == ["Disk /a is 95% full"]


def test_the_real_reader_reports_a_percentage() -> None:
    from apps.platform.disk import read_usage

    reading = read_usage("/")
    assert 0.0 <= reading.percent_used <= 100.0
