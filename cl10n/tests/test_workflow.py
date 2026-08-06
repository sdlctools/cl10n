"""`.github/workflows/cl10n.yml` — the CI half of the pipeline's contract.

No other test loads the workflow, and the properties it encodes are exactly the
kind that fail silently: a missing token scope degrades to an empty recovery,
a concurrency guard that stops guarding shows up as a corrupted translation
memory much later, and a secret leaking into a second step is invisible until
someone reads a log. Each acceptance criterion that lives in YAML rather than
in Python gets one assertion here.

These are contract assertions, not a workflow runner — they parse the file and
check what it declares.
"""

from __future__ import annotations

import os

import pytest

yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "cl10n.yml")


@pytest.fixture(scope="module")
def workflow() -> dict:
    with open(WORKFLOW, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def triggers(workflow) -> dict:
    # PyYAML reads a bare `on:` key as the YAML 1.1 boolean True.
    return workflow.get("on", workflow.get(True))


@pytest.fixture(scope="module")
def steps(workflow) -> list[dict]:
    return workflow["jobs"]["localize"]["steps"]


def test_the_token_may_read_artifacts(workflow):
    """AC4, recovery layer 3 — the crash artifact must be readable.

    Declaring a `permissions` block sets every scope not listed in it to
    `none`, so the artifacts REST API the recovery step calls needs `actions`
    named explicitly. Without it the step 403s and reports "no artifact",
    which is indistinguishable from the ordinary first-run case — the upload
    keeps working (it uses the runtime token), so the layer looks alive while
    contributing nothing.
    """
    assert workflow["permissions"].get("actions") == "read"


def test_the_workflow_can_write_the_branch_and_open_the_pr(workflow):
    assert workflow["permissions"].get("contents") == "write"
    assert workflow["permissions"].get("pull-requests") == "write"


def test_the_trigger_is_limited_to_the_watched_corpus(triggers):
    """AC2 — a push touching no watched Markdown starts nothing."""
    assert triggers["push"]["paths"] == ["md/**"]
    assert triggers["push"]["branches"] == ["development"]
    # Manual dispatch resumes an interrupted run without a dummy commit.
    assert "workflow_dispatch" in triggers


def test_rapid_pushes_serialize_instead_of_racing(workflow):
    """AC5 — two pushes must not both write the translation memory."""
    concurrency = workflow["concurrency"]
    assert concurrency["group"] == "cl10n"
    assert concurrency["cancel-in-progress"] is False


def test_the_provider_secret_reaches_exactly_one_step(steps):
    """AC6/AC7 — every provider key is env of the Execute step and nothing else.

    Single-secret-per-run is preserved in the form that matters for fork safety
    (CLN-1): exactly one step ever holds a provider key, and the repo never
    gains a pull_request trigger. The runner reads only its active connector's
    `api_key_env`, so a key bound to the step but unused this run sits unread.
    This asserts that shape — both declared keys on the one Execute step — so a
    future provider's secret landing on a second step would fail here.
    """
    holders = [
        step["name"] for step in steps
        if any(k in str(step.get("env", {}))
               for k in ("GROQ_API_KEY", "NVIDIA_NIM_API_KEY", "MISTRAL_API_KEY"))
    ]
    assert holders == ["Execute the queue"]


def test_every_declared_provider_has_its_secret_wired(steps):
    """A provider in `providers.toml` whose key never reaches CI is a trap.

    It works locally (creds file), then every CI run rejects every job with an
    auth error the moment someone routes to it. Cheap to assert, so assert it.
    """
    import tomllib

    with open(os.path.join(REPO, "cl10n", "providers.toml"), "rb") as fh:
        declared = tomllib.load(fh)["providers"]
    execute = next(s for s in steps if s["name"] == "Execute the queue")
    env = execute.get("env", {})
    for name, cfg in declared.items():
        assert cfg["api_key_env"] in env, (
            f"provider {name!r} declares {cfg['api_key_env']} but the Execute "
            f"step does not pass it"
        )


def test_no_fork_triggered_event_can_reach_the_secret(triggers):
    """AC6 — code from a fork must never execute where the secret lives."""
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_translations_arrive_as_a_pull_request(steps):
    """AC3 — never a direct push to the default branch."""
    pr_step = next(s for s in steps if s["name"] == "Open or update the pull request")
    assert "gh pr create" in pr_step["run"]
    assert 'git push --force origin "$PR_BRANCH"' in pr_step["run"]
    # The only push target is the dedicated branch, never the trigger's branch.
    assert "git push origin HEAD" not in pr_step["run"]


def test_the_crash_artifact_is_uploaded_even_when_the_run_dies(steps):
    """AC4, recovery layer 3 — the upload must not be conditional on success."""
    upload = next(
        s for s in steps if s.get("uses", "").startswith("actions/upload-artifact")
    )
    assert upload["if"].startswith("always()")
    assert upload["with"]["name"] == "cl10n-tm"


def test_execution_is_time_boxed_and_does_not_abort_the_run(workflow, steps):
    """AC4 — a run that stops early still renders and ships what completed."""
    execute = next(s for s in steps if s["name"] == "Execute the queue")
    assert execute["continue-on-error"] is True
    # The step's box must leave the job room to render and open the PR.
    assert execute["timeout-minutes"] < workflow["jobs"]["localize"]["timeout-minutes"]
