"""The compose stack, checked against the application it is supposed to run.

A stack whose scrape path, database file or dashboard has drifted from the code fails quietly:
empty Grafana panels, a viewer showing nothing, a Prometheus target permanently down. None of
that is visible until someone demonstrates the system. These tests are cheap and offline, and
they are the only thing standing between a working `docker compose up` and a broken one.

They do **not** run Docker. What they check is that every path, port and file the compose file
names is the one the application actually uses.
"""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text("utf-8"))
SERVICES = COMPOSE["services"]


def test_the_stack_runs_the_api_and_the_three_windows_into_it():
    assert set(SERVICES) >= {"api", "decisions", "prometheus", "grafana"}
    assert "8000:8000" in SERVICES["api"]["ports"]
    # The one-off jobs are behind a profile, so `docker compose up` does not run a training job
    # or a full evaluation by surprise.
    for name in ("train", "gate"):
        assert SERVICES[name]["profiles"] == ["tools"], name


def test_no_service_bakes_a_key_into_an_image():
    """CLAUDE.md: no keys in code, tests, fixtures or history — and none in an image either."""
    ignored = (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    assert ".env" in ignored, "the build context must not carry .env"

    for name, service in SERVICES.items():
        assert "LLM_API_KEY" not in (service.get("environment") or {}), name
        if "build" in service:
            assert service.get("env_file") == [".env"], f"{name} reads the key at run time"
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    assert "LLM_API_KEY" not in dockerfile


def test_the_viewer_is_read_only_and_points_at_the_decision_log():
    """NFR-05: this is the audit record. A viewer that can edit it is not an audit record."""
    command = SERVICES["decisions"]["command"]
    assert "-r" in command, "sqlite-web must run read-only"
    assert any(part.endswith("decisions.db") for part in command)

    # The viewer and the API must share one volume, or the viewer shows an empty database.
    api_volumes = {v.split(":")[0] for v in SERVICES["api"]["volumes"]}
    viewer_volumes = {v.split(":")[0] for v in SERVICES["decisions"]["volumes"]}
    assert "storage" in api_volumes & viewer_volumes

    log_path = SERVICES["api"]["environment"]["DECISION_LOG_PATH"]
    mount = next(v for v in SERVICES["decisions"]["volumes"] if v.startswith("storage:"))
    in_container = mount.split(":")[1]
    assert Path(log_path).name == "decisions.db"
    assert any(str(p).endswith(in_container.lstrip("/")) or in_container in str(p)
               for p in [Path(c) for c in command if c.startswith("/")])


def test_prometheus_scrapes_the_path_the_api_actually_serves():
    """A target that 404s is a dashboard of empty panels nobody notices for a week."""
    from ticketing_agent.api import build_app
    from ticketing_agent.config import Settings

    scrape = yaml.safe_load((ROOT / "ops" / "prometheus.yml").read_text("utf-8"))
    job = scrape["scrape_configs"][0]
    assert job["metrics_path"] == "/metrics/prometheus"
    assert job["static_configs"][0]["targets"] == ["api:8000"], (
        "the target must be the service name and the port the API listens on")

    app = build_app(Settings(model_name="t", docs_path=ROOT / "data" / "documentation.json"))
    served = {route.path for route in app.routes}
    assert job["metrics_path"] in served, "Prometheus would scrape a path the API does not serve"


def test_grafana_is_provisioned_with_the_dashboard_this_repository_owns():
    datasource = yaml.safe_load(
        (ROOT / "ops" / "grafana" / "provisioning" / "datasources" / "prometheus.yml")
        .read_text("utf-8"))["datasources"][0]
    assert datasource["url"] == "http://prometheus:9090", "must match the compose service name"

    provider = yaml.safe_load(
        (ROOT / "ops" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml")
        .read_text("utf-8"))["providers"][0]
    mounted = next(v for v in SERVICES["grafana"]["volumes"]
                   if "grafana_dashboard.json" in v)
    container_path = mounted.split(":")[1]
    assert container_path.startswith(provider["options"]["path"]), (
        "the dashboard is mounted outside the directory Grafana is told to read")
    assert json.loads((ROOT / "ops" / "grafana_dashboard.json").read_text("utf-8"))["panels"]


def test_every_host_file_the_stack_mounts_exists():
    """A missing bind mount becomes an empty directory in the container, silently."""
    for name, service in SERVICES.items():
        for volume in service.get("volumes", []):
            source = volume.split(":")[0]
            if source.startswith("./"):
                assert (ROOT / source[2:]).exists(), f"{name} mounts a missing path: {source}"


def test_the_image_copies_everything_the_package_metadata_needs():
    """`pyproject` names files that must exist when the wheel is built. A missing one fails at
    the *second* `uv sync`, several minutes into a build, which is a poor place to find out."""
    import tomllib

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    copied = " ".join(line for line in dockerfile.splitlines() if line.startswith("COPY "))

    readme = pyproject["project"].get("readme")
    if isinstance(readme, str):
        assert readme in copied, f"{readme} is declared in pyproject and never copied"
    for package in pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]:
        top = package.split("/")[0]
        assert f"{top}/" in copied, f"the wheel needs {package} and the image does not copy it"


def test_the_kill_switch_is_reachable_from_the_host():
    """FR-16 is operated by creating a file. If it lives inside the image an operator cannot
    touch it without entering the container, which is not an emergency control."""
    switch = SERVICES["api"]["environment"]["KILL_SWITCH_FILE"]
    assert switch.startswith("/app/storage/"), switch
    assert any(v.startswith("storage:/app/storage") for v in SERVICES["api"]["volumes"])
