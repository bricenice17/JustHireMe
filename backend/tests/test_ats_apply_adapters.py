from __future__ import annotations

import json
from pathlib import Path

import packet_cli
from ats_apply_adapters import detect_ats, prepare_apply


CANDIDATE = {
    "name": "Mike Matican",
    "email": "mike@example.com",
    "phone": "555-0100",
    "linkedin_url": "https://linkedin.com/in/mike",
    "github": "https://github.com/bricenice17",
    "website": "https://example.com",
    "current_company": "Matican",
    "cover_letter": "I am interested in this role.",
    "resume_path": "/tmp/resume.pdf",
}


def _fixture(tmp_path: Path, name: str, html: str) -> Path:
    path = tmp_path / name
    path.write_text(html, encoding="utf-8")
    return path


def test_detect_supported_ats_families():
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/123") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/acme/123") == "lever"
    assert detect_ats("https://jobs.ashbyhq.com/acme/123") == "ashby"


def test_greenhouse_dry_run_maps_fields_and_writes_audit(tmp_path: Path):
    fixture = _fixture(
        tmp_path,
        "greenhouse.html",
        """
        <form id="application_form">
          <label for="first_name">First name</label><input id="first_name" name="first_name" required>
          <label for="last_name">Last name</label><input id="last_name" name="last_name" required>
          <label for="email">Email</label><input id="email" name="email" type="email" required>
          <label for="phone">Phone</label><input id="phone" name="phone">
          <label for="linkedin">LinkedIn</label><input id="linkedin" name="question[linkedin_profile]">
          <label for="resume">Resume</label><input id="resume" name="resume" type="file" required>
          <button type="submit">Submit Application</button>
        </form>
        """,
    )
    audit_path = tmp_path / "audit.json"

    result = prepare_apply(
        "https://boards.greenhouse.io/acme/jobs/123",
        candidate=CANDIDATE,
        mode="dry_run",
        fixture_html=fixture,
        audit_path=audit_path,
    )

    assert result["status"] == "applied_dry_run"
    assert result["submitted"] is False
    assert result["submit_attempted"] is False
    assert {field["type"] for field in result["fields_mapped"]} >= {"first_name", "last_name", "email", "phone", "linkedin_url", "resume"}
    assert result["block_reasons"] == []
    assert json.loads(audit_path.read_text())["next_human_action"].startswith("Review mapped")


def test_lever_prepare_fills_no_submit_and_marks_manual_review(tmp_path: Path):
    fixture = _fixture(
        tmp_path,
        "lever.html",
        """
        <form>
          <label>Name<input name="name" required></label>
          <label>Email<input name="email" type="email" required></label>
          <label>Phone<input name="phone"></label>
          <label>Current company<input name="org"></label>
          <label>LinkedIn<input name="urls[LinkedIn]"></label>
          <label>Portfolio<input name="urls[Portfolio]"></label>
          <label>Additional information<textarea name="comments"></textarea></label>
          <button type="submit">Submit application</button>
        </form>
        """,
    )

    result = prepare_apply("https://jobs.lever.co/acme/abc", candidate=CANDIDATE, mode="prepare", fixture_html=fixture)

    assert result["status"] == "filled_no_submit"
    assert result["ready_for_manual_submit"] is True
    assert result["submitted"] is False
    assert result["next_human_action"] == "Review the prepared ATS form in browser and manually click final submit if everything is correct."
    assert "ready_for_manual_submit" in result["safe_status_outcomes"]


def test_ashby_blocks_custom_visa_or_salary_question(tmp_path: Path):
    fixture = _fixture(
        tmp_path,
        "ashby.html",
        """
        <form>
          <label>Name<input name="name" required></label>
          <label>Email<input name="email" type="email" required></label>
          <label>Will you now or in the future require visa sponsorship?<select name="sponsorship" required><option></option></select></label>
          <label>Desired salary<input name="salary" required></label>
          <button>Submit</button>
        </form>
        """,
    )

    result = prepare_apply("https://jobs.ashbyhq.com/acme/role", candidate=CANDIDATE, mode="prepare", fixture_html=fixture)

    assert result["status"] == "blocked_custom_question"
    assert result["submitted"] is False
    assert any(block["code"] in {"visa_question", "salary_question", "custom_question"} for block in result["block_reasons"])
    assert result["next_human_action"].startswith("Human must resolve")


def test_captcha_blocks_before_manual_submit(tmp_path: Path):
    fixture = _fixture(
        tmp_path,
        "greenhouse_captcha.html",
        """
        <form>
          <label>Email<input name="email" type="email" required></label>
          <div class="g-recaptcha">captcha</div>
          <button type="submit">Submit</button>
        </form>
        """,
    )

    result = prepare_apply("https://boards.greenhouse.io/acme/jobs/456", candidate=CANDIDATE, mode="prepare", fixture_html=fixture)

    assert result["status"] == "blocked_captcha"
    assert any(block["code"] == "captcha" for block in result["block_reasons"])


def test_unsupported_url_returns_unsupported_without_fetching():
    result = prepare_apply("https://example.com/jobs/123", candidate=CANDIDATE, mode="dry_run")

    assert result["status"] == "unsupported"
    assert result["fields_mapped"] == []
    assert result["block_reasons"][0]["code"] == "unsupported"


def test_packet_cli_apply_subcommand_outputs_json_and_audit(tmp_path: Path, capsys):
    fixture = _fixture(
        tmp_path,
        "lever_cli.html",
        """
        <form>
          <label>Name<input name="name" required></label>
          <label>Email<input name="email" type="email" required></label>
          <button type="submit">Submit application</button>
        </form>
        """,
    )
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(json.dumps(CANDIDATE), encoding="utf-8")
    audit_path = tmp_path / "cli-audit.json"

    code = packet_cli.main([
        "apply",
        "--prepare",
        "--url",
        "https://jobs.lever.co/acme/abc",
        "--fixture-html",
        str(fixture),
        "--candidate-json",
        str(candidate_path),
        "--audit-path",
        str(audit_path),
    ])

    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "filled_no_submit"
    assert output["submitted"] is False
    assert json.loads(audit_path.read_text())["fields_mapped"]
