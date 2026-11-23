"""Static checks on the repository's CI and image definition (DACC-BUILD-001, DACC-BUILD-002).

Ported from obsidian-git-mcp (chaosbit-dev/obsidian-git-mcp#2). The workload's manifests live in
gonk-infra and are checked there, not here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DOCKERFILE = ROOT / "Dockerfile"

SHA_REF = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
DIGEST_REF = re.compile(r"@sha256:[0-9a-f]{64}$")
BUILD_ACTION = "docker/build-push-action@"
DOCKER_RUN = re.compile(r"\bdocker\s+(buildx\s+)?(build|push)\b")
STORED_SECRET = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")


# --- helpers ---------------------------------------------------------------------------------


def workflow_files() -> list[Path]:
    if not WORKFLOWS.is_dir():
        return []
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def load(path: Path) -> dict[str, Any]:
    data = YAML(typ="safe").load(path.read_text())
    assert isinstance(data, dict), f"{path.name} is not a mapping"
    return data


def jobs_of(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs") or {}
    assert isinstance(jobs, dict)
    return jobs


def steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    assert isinstance(steps, list)
    return steps


def all_uses() -> list[tuple[str, str]]:
    """(workflow file, uses value) for every step- and job-level `uses:`."""
    found: list[tuple[str, str]] = []
    for path in workflow_files():
        for job in jobs_of(load(path)).values():
            if "uses" in job:
                found.append((path.name, str(job["uses"])))
            found.extend((path.name, str(s["uses"])) for s in steps_of(job) if "uses" in s)
    return found


def local_workflow(uses: str) -> Path | None:
    if uses.startswith("./"):
        return ROOT / uses.removeprefix("./").split("@", 1)[0]
    return None


def runs_tests(job: dict[str, Any], seen: frozenset[Path] = frozenset()) -> bool:
    """The job runs pytest itself, or calls a local reusable workflow that does."""
    if any("pytest" in str(s.get("run", "")) for s in steps_of(job)):
        return True
    called = local_workflow(str(job.get("uses", "")))
    if called is None or called in seen or not called.is_file():
        return False
    return any(runs_tests(j, seen | {called}) for j in jobs_of(load(called)).values())


def builds_image(job: dict[str, Any]) -> bool:
    """Whether the job publishes an image: DACC-BUILD-001 constrains building *and pushing*.

    A build-push step counts only when it pushes, so a pull request's build-only dry run does
    not; any `docker push` (or `docker build` in a run step, which cannot be told apart) does.
    """
    for step in steps_of(job):
        pushes = str((step.get("with") or {}).get("push", "false")).lower() == "true"
        if str(step.get("uses", "")).startswith(BUILD_ACTION) and pushes:
            return True
        if DOCKER_RUN.search(str(step.get("run", ""))):
            return True
    return False


def needs_of(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else [str(n) for n in needs]


def upstream(jobs: dict[str, dict[str, Any]], name: str) -> set[str]:
    """Every job `name` waits for, transitively."""
    found: set[str] = set()
    pending = needs_of(jobs[name])
    while pending:
        current = pending.pop()
        if current in found or current not in jobs:
            continue
        found.add(current)
        pending.extend(needs_of(jobs[current]))
    return found


def image_build_jobs() -> list[tuple[Path, str, dict[str, dict[str, Any]]]]:
    return [
        (path, name, jobs)
        for path in workflow_files()
        for jobs in [jobs_of(load(path))]
        for name, job in jobs.items()
        if builds_image(job)
    ]


def from_images(dockerfile: str) -> list[str]:
    """The image each FROM names, with ARG defaults substituted; stage references dropped."""
    args: dict[str, str] = {}
    stages: set[str] = set()
    images: list[str] = []
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        if keyword.upper() == "ARG":
            name, _, default = rest.strip().partition("=")
            args[name.strip()] = default.strip().strip("\"'")
        elif keyword.upper() == "FROM":
            words = [w for w in rest.split() if not w.startswith("--")]
            image = re.sub(r"\$\{?(\w+)\}?", lambda m: args.get(m.group(1), m.group(0)), words[0])
            if len(words) >= 3 and words[1].upper() == "AS":
                stages.add(words[2].lower())
            if image.lower() not in stages and image != "scratch":
                images.append(image)
    return images


# --- DACC-BUILD-001 ------------------------------------------------------------------------------


# @spec DACC-BUILD-001
def test_every_remote_action_is_pinned_to_a_commit_sha() -> None:
    uses = all_uses()
    assert uses, "no workflow uses any action"
    unpinned = [
        (name, ref)
        for name, ref in uses
        if local_workflow(ref) is None
        and not (ref.startswith("docker://") and DIGEST_REF.search(ref))
        and not SHA_REF.match(ref)
    ]
    assert unpinned == []


# @spec DACC-BUILD-001
def test_a_workflow_builds_and_pushes_the_image() -> None:
    assert image_build_jobs(), "no workflow job builds the server image"


# @spec DACC-BUILD-001
def test_image_build_job_waits_for_a_job_that_runs_the_tests() -> None:
    builds = image_build_jobs()
    assert builds, "no workflow job builds the server image"
    for path, name, jobs in builds:
        gates = [j for j in upstream(jobs, name) if runs_tests(jobs[j])]
        assert gates, f"{path.name}:{name} builds the image without needing a test job"


# @spec DACC-BUILD-002
def test_dockerfile_base_images_are_referenced_by_digest() -> None:
    assert DOCKERFILE.is_file(), "no Dockerfile at the repository root"
    images = from_images(DOCKERFILE.read_text())
    assert images, "Dockerfile names no base image"
    undigested = [image for image in images if not DIGEST_REF.search(image)]
    assert undigested == []


# @spec DACC-BUILD-001
def test_the_image_is_published_to_ghcr_and_signed() -> None:
    builds = image_build_jobs()
    assert builds, "no workflow job builds the server image"
    for path, name, jobs in builds:
        job = jobs[name]
        text = path.read_text()
        assert "ghcr.io/chaosbit-dev/paperless-mcp" in text, f"{path.name} does not push to GHCR"
        perms = job.get("permissions") or {}
        assert perms.get("packages") == "write", f"{path.name}:{name} lacks packages: write"
        assert perms.get("id-token") == "write", f"{path.name}:{name} cannot sign keyless"
        runs = " ".join(str(s.get("run", "")) for s in steps_of(job))
        assert "cosign sign" in runs, f"{path.name}:{name} does not sign the image"


# @spec DACC-BUILD-001
def test_no_workflow_holds_a_stored_secret_or_reaches_the_private_registry() -> None:
    """Public repo: only the run's own GITHUB_TOKEN, no secret-holding environment, no tailnet."""
    for path in workflow_files():
        text = path.read_text()
        stored = set(STORED_SECRET.findall(text)) - {"GITHUB_TOKEN"}
        assert stored == set(), f"{path.name} reads stored secrets {sorted(stored)}"
        assert "registry.home.chaosbit.dev" not in text, f"{path.name} names the private registry"
        assert "tailscale" not in text.lower(), f"{path.name} joins the tailnet"
        for name, job in jobs_of(load(path)).items():
            env = job.get("environment")
            env_name = env.get("name") if isinstance(env, dict) else env
            # The one environment allowed is github-pages: it holds no secrets, and its branch
            # policy is what stops a workflow run from any branch but main deploying Pages.
            assert env is None or env_name == "github-pages", (
                f"{path.name}:{name} uses deployment environment {env_name!r}"
            )


# @spec DACC-BUILD-001
def test_pull_requests_publish_nothing() -> None:
    for path, name, jobs in image_build_jobs():
        condition = str(jobs[name].get("if", ""))
        assert "github.event_name != 'pull_request'" in condition, (
            f"{path.name}:{name} pushes an image without excluding pull requests"
        )


# The helpers decide what counts as pinned and as a gate; a wrong helper would pass a bad repo.


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM python:3.13-slim\n", ["python:3.13-slim"]),
        (
            "ARG BASE=python:3.13@sha256:" + "a" * 64 + "\nFROM --platform=$X ${BASE} AS build\n"
            "FROM build\nFROM scratch\n",
            ["python:3.13@sha256:" + "a" * 64],
        ),
    ],
)
def test_from_images_resolves_args_and_skips_stages(dockerfile: str, expected: list[str]) -> None:
    assert from_images(dockerfile) == expected


def test_runs_tests_follows_a_local_reusable_workflow() -> None:
    ci = WORKFLOWS / "ci.yml"
    if not ci.is_file():
        pytest.fail("ci.yml is missing")
    assert runs_tests({"uses": "./.github/workflows/ci.yml"})
    assert not runs_tests({"steps": [{"run": "uv run ruff check ."}]})
