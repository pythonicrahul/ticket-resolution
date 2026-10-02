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


def _env_names(service: dict) -> set[str]:
    """The variable names a service sets, whichever form compose allows.

    Mapping: `{"KEY": "value"}`. List: `["KEY=value", "KEY"]`. A guard that only handles the
    mapping form silently passes the list form, which is how a key could have been baked in.
    """
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return set(env)
    return {str(item).split("=", 1)[0] for item in env}


def test_the_env_name_reader_handles_both_compose_forms():
    """The helper the key guard rests on, because the guard was wrong for one of two forms."""
    assert _env_names({"environment": {"LLM_API_KEY": "x"}}) == {"LLM_API_KEY"}
    assert _env_names({"environment": ["LLM_API_KEY=sk-live-whatever"]}) == {"LLM_API_KEY"}
    assert _env_names({"environment": ["LLM_API_KEY"]}) == {"LLM_API_KEY"}
    assert _env_names({}) == set()


def test_no_service_bakes_a_key_into_an_image():
    """CLAUDE.md: no keys in code, tests, fixtures or history — and none in an image either."""
    ignored = (ROOT / ".dockerignore").read_text("utf-8").splitlines()
    assert ".env" in ignored, "the build context must not carry .env"

    for name, service in SERVICES.items():
        # Compose accepts `environment` as a mapping **or** a list of "KEY=value" strings, and
        # `"LLM_API_KEY" not in ["LLM_API_KEY=sk-live-..."]` is True — so the list form sailed
        # past the guard that exists to stop exactly that (R10 review).
        assert _env_names(service) .isdisjoint({"LLM_API_KEY"}), (
            f"{name} bakes the key into the image")
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
    # The same host directory, not merely the same-looking name: the api writes to a bind
    # mount for FR-16's sake, so a viewer still on the named volume would read an empty one.
    assert "./storage" in api_volumes & viewer_volumes, (api_volumes, viewer_volumes)

    log_path = SERVICES["api"]["environment"]["DECISION_LOG_PATH"]
    mount = next(v for v in SERVICES["decisions"]["volumes"]
                 if v.split(":")[0] == "./storage")
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
    """FR-16 is operated by creating a file, so the file has to be reachable.

    This asserted a **named** volume (`storage:/app/storage`), which lives inside Docker's own
    storage area — inside the VM on macOS — and cannot be `touch`ed from the host at all. So
    the test demanded the one arrangement that makes its own docstring false, and changing it
    to a bind mount made the test fail (R10 review). A bind mount (`./storage:/app/storage`)
    is the only form where `touch storage/KILL_SWITCH` works from the host, which is what the
    README promises.
    """
    switch = SERVICES["api"]["environment"]["KILL_SWITCH_FILE"]
    assert switch.startswith("/app/storage/"), switch
    mounts = [v for v in SERVICES["api"]["volumes"] if v.endswith(":/app/storage")]
    assert mounts, SERVICES["api"]["volumes"]
    host_side = mounts[0].split(":")[0]
    assert host_side.startswith(("./", "/")), (
        f"{host_side!r} is a named volume, which the host cannot reach; FR-16 needs a bind "
        f"mount for `touch storage/KILL_SWITCH` to work as the README says it does")
    # And the host path the operator types resolves to the repository's own storage directory.
    assert (ROOT / host_side.removeprefix("./")).name == "storage"


def test_T_R10_8_the_image_copies_every_directory_the_runtime_reads():
    """R10 (MEDIUM): the image test checked only what `pyproject` declares.

    `packages = ["src/ticketing_agent"]`, so deleting `COPY prompts/` or `COPY scripts/` from
    the Dockerfile kept the suite green and produced an image where every model call raises
    `PromptError` — while this module's docstring claims the tests check "that every path, port
    and file the compose file names is the one the application actually uses".

    Derived from what the code actually reads, not from a list someone remembers to update.
    """
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    copied = {line.split()[1].rstrip("/")
              for line in dockerfile.splitlines() if line.startswith("COPY ") and
              not line.startswith("COPY --from")}

    # `prompts/` is loaded by name at runtime (`prompts.load`), `evaluation/` is the harness the
    # `gate` service runs, and `scripts/` holds the training entry point the `train` service runs.
    for needed, why in (
        ("src", "the package itself"),
        ("prompts", "prompts.load reads these at runtime; without them every model call raises"),
        ("evaluation", "the `gate` service runs `python -m evaluation.harness`"),
        ("scripts", "the `train` service runs `python scripts/train_classifier.py`"),
    ):
        assert needed in copied, f"the image does not copy {needed}/: {why}"

    # And every compose command's entry point exists in something the image copies.
    for name, service in SERVICES.items():
        command = service.get("command") or []
        if isinstance(command, str) or "build" not in service:
            continue
        for part in command:
            if str(part).endswith(".py"):
                top = str(part).split("/")[0]
                assert top in copied, f"{name} runs {part} and the image has no {top}/"


def test_T_R10_9_the_evaluation_inputs_do_not_ship_in_the_image():
    """R10 (MEDIUM): `COPY data/` put the agents' private answer files in the serving image.

    FR-04 §3.1 singles out the agents' own answers as "never indexed and never returned", and
    the Dataset Guide says a system that could read them would make the evaluation meaningless.
    Nothing in `src/` reads them, so this was exposure rather than a leak — but the only thing
    keeping them out of the index was `DOCS_PATH` pointing elsewhere, and a `.dockerignore`
    entry removes the question (D-79).
    """
    ignored = [ln.strip() for ln in (ROOT / ".dockerignore").read_text("utf-8").splitlines()]
    assert "data/ground_truth_responses.json" in ignored, (
        "the agents' own answers must not be in the build context")
    assert "data/validation_tickets.json" in ignored

    # The documentation corpus still has to reach the image: it is what FR-04 searches.
    assert "data/documentation.json" not in ignored
    assert SERVICES["api"]["environment"]["DOCS_PATH"] == "/app/data/documentation.json"

    # And the service that does need the evaluation inputs mounts them instead.
    gate_mounts = {v.split(":")[0] for v in SERVICES["gate"]["volumes"]}
    assert "./data" in gate_mounts, (
        "the gate run is pointed at a file; mounting ./data read-only also means an unseen "
        "file can be dropped in and named with --input rather than editing compose")
    assert any(v.startswith("./data:") and v.endswith(":ro")
               for v in SERVICES["gate"]["volumes"]), "read-only: a run must not alter its input"
