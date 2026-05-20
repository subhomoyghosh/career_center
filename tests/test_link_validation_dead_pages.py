"""Tests for dead-page detection in link_validation._body_parseable_and_not_dead.

Covers the two leak classes that motivated the regex + visible-text checks:
- builtin.com "Sorry, this job was removed at ..." soft-404 wording.
- Workday-style SPA shells: kilobytes of HTML markup, zero visible text.
"""
from __future__ import annotations

import pytest

from job_finder.link_validation import (
    MIN_BODY_CHARS_RELAXED,
    _body_parseable_and_not_dead,
    _visible_text_len,
)


class _FakeResponse:
    """Minimum surface that _body_parseable_and_not_dead reads from."""

    def __init__(self, text: str, status_code: int = 200, url: str = "https://example.com/job/123"):
        self.text = text
        self.status_code = status_code
        self.url = url

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


# --- visible_text helper ----------------------------------------------------


def test_visible_text_len_ignores_scripts_and_tags():
    html = "<html><head><script>var x=1;</script><style>.a{}</style></head><body><p>hello world</p></body></html>"
    assert _visible_text_len(html) == len("hello world")


def test_visible_text_len_spa_shell_is_zero():
    shell = "<html><head><title></title></head><body><script>app.init()</script></body></html>"
    assert _visible_text_len(shell) == 0


# --- builtin.com "was removed" wording -------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "Sorry, this job was removed at 06:07 p.m. (CST) on Wednesday, Oct 01, 2025",
        "This position was removed by the employer.",
        "This listing was removed.",
        "This posting has been removed.",
        "These jobs were removed last week.",
    ],
)
def test_removed_wording_variants_marked_dead(phrase: str):
    # Pad to comfortably exceed MIN_BODY_CHARS_RELAXED with visible text so the
    # length gates don't short-circuit before the phrase check.
    body = (
        f"<html><body><h1>Senior Data Scientist</h1>"
        f"<p>{phrase}</p>"
        f"<p>{'About this role. ' * 60}</p>"
        f"</body></html>"
    )
    r = _FakeResponse(body)
    assert _body_parseable_and_not_dead(
        r,
        job_title="Senior Data Scientist",
        min_body_chars=MIN_BODY_CHARS_RELAXED,
    ) is False


# --- Workday SPA shell -----------------------------------------------------


def test_spa_shell_dropped_despite_passing_raw_length():
    shell = (
        "<!doctype html><html><head><title></title>"
        + "<script>" + ("x=1;" * 400) + "</script>"
        + "</head><body><script>app.init()</script></body></html>"
    )
    assert len(shell.strip()) > MIN_BODY_CHARS_RELAXED  # raw length passes
    assert _visible_text_len(shell) == 0  # but visible text is empty
    r = _FakeResponse(shell)
    assert _body_parseable_and_not_dead(
        r,
        job_title="Senior Data Scientist",
        min_body_chars=MIN_BODY_CHARS_RELAXED,
    ) is False


# --- regression guard: real-looking job posting still passes ---------------


def test_real_job_posting_still_passes():
    body = (
        "<html><body>"
        "<h1>Senior Data Scientist</h1>"
        "<p>"
        + ("We are hiring a senior data scientist to lead causal inference work. "
           * 30)
        + "</p>"
        "<p>Responsibilities include modeling, experimentation, and reporting.</p>"
        "<button>Apply</button>"
        "</body></html>"
    )
    r = _FakeResponse(body)
    assert _body_parseable_and_not_dead(
        r,
        job_title="Senior Data Scientist",
        min_body_chars=MIN_BODY_CHARS_RELAXED,
    ) is True


# --- regression guard: pre-existing "has been removed" still caught --------


def test_existing_has_been_removed_phrase_still_caught():
    body = (
        "<html><body><h1>Senior Data Scientist</h1>"
        "<p>This job has been removed by the employer.</p>"
        f"<p>{'Lorem ipsum dolor sit amet. ' * 50}</p>"
        "</body></html>"
    )
    r = _FakeResponse(body)
    assert _body_parseable_and_not_dead(
        r,
        job_title="Senior Data Scientist",
        min_body_chars=MIN_BODY_CHARS_RELAXED,
    ) is False
