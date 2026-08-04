"""`cl10n/compat_check.py` — the markdown-it-py / mdformat drift detector.

Two things need testing here, and the second is the one that matters. That the
installed libraries currently agree with the recorded baseline is a fact about
this machine. That the detector *would notice* if they stopped is a fact about
the detector, and it is the reason the file exists — a drift check nobody has
ever seen fail is indistinguishable from one that cannot fail.
"""

from __future__ import annotations

import copy
import os
import sys

CL10N = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(CL10N)
sys.path[:0] = [CL10N, os.path.join(REPO, "app")]

import compat_check  # noqa: E402


def test_the_installed_libraries_match_the_recorded_baseline():
    """The check the CI job runs. A failure here is a real dependency drift."""
    assert compat_check.main([]) == 0


def test_the_fixture_is_its_own_canonical_form():
    """Otherwise 'the fixture changed' and 'a normalisation changed' look alike."""
    observed = compat_check.observe()
    assert observed["fixture_is_stable"]
    assert observed["fixture_is_idempotent"]


# --------------------------------------------------------------------------
# The detector detects — one case per drift class
# --------------------------------------------------------------------------


def _baseline():
    return compat_check.load_baseline()


def test_it_catches_a_new_parser_option():
    """The earliest signal: `gfm-like2` gaining `alerts` looked exactly like this.

    A preset that starts carrying an option we have never seen is drift even
    before any document exercises it — which is the whole point of watching the
    option surface rather than waiting for a render to raise.
    """
    baseline = _baseline()
    assert "footnotes" not in baseline["options"], "pick an option that does not exist yet"
    current = copy.deepcopy(baseline)
    current["options"]["footnotes"] = "True"

    findings = compat_check.compare(baseline, current)

    assert any("NEW `footnotes`" in f for f in findings)


def test_it_catches_a_changed_parser_option():
    current = copy.deepcopy(_baseline())
    current["options"]["tasklists"] = "True"

    findings = compat_check.compare(_baseline(), current)

    assert any("`tasklists` changed" in f for f in findings)


def test_it_catches_a_node_type_that_lost_its_renderer():
    """The crash class: a node type mdformat cannot render raises on any document."""
    current = copy.deepcopy(_baseline())
    current["node_types_without_renderer"] = ["alert"]

    findings = compat_check.compare(_baseline(), current)

    assert any("`alert` has no mdformat renderer" in f for f in findings)


def test_it_catches_a_silent_canonicalisation_change():
    """The expensive class: nothing raises, every hash in the corpus moves."""
    current = copy.deepcopy(_baseline())
    current["fixture_canonical_sha256"] = "0" * 64

    findings = compat_check.compare(_baseline(), current)

    assert any("canonical bytes" in f and "orphans existing translations" in f
               for f in findings)


def test_it_catches_moved_unit_hashes():
    current = copy.deepcopy(_baseline())
    current["fixture_unit_hashes"] = current["fixture_unit_hashes"][:-1]

    findings = compat_check.compare(_baseline(), current)

    assert any("unit hash(es) moved" in f for f in findings)


def test_it_catches_canonicalisation_becoming_unstable():
    current = copy.deepcopy(_baseline())
    current["fixture_is_idempotent"] = False

    findings = compat_check.compare(_baseline(), current)

    assert any("no longer idempotent" in f for f in findings)


def test_a_version_bump_alone_is_context_not_a_finding():
    """An upgrade is the *cause* of drift; on its own it is not drift.

    Failing on the version alone would make every dependency bump red and train
    people to re-record the baseline without reading it, which is precisely how
    a real canonicalisation change would get waved through.
    """
    current = copy.deepcopy(_baseline())
    current["versions"]["markdown-it-py"] = "99.0.0"

    assert compat_check.compare(_baseline(), current) == []
    assert any("markdown-it-py" in line
               for line in compat_check.report_versions(_baseline(), current))
