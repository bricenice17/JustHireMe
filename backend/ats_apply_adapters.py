from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SUPPORTED_ATS = {"greenhouse", "lever", "ashby"}
SAFE_STATUS_OUTCOMES = {
    "ready_for_manual_submit",
    "blocked_captcha",
    "blocked_custom_question",
    "unsupported",
    "applied_dry_run",
    "filled_no_submit",
}

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first_name", "firstname", "first name", "given-name", "given name"),
    "last_name": ("last_name", "lastname", "last name", "family-name", "family name"),
    "name": ("full_name", "fullname", "name", "legal name"),
    "email": ("email", "e-mail"),
    "phone": ("phone", "mobile", "tel", "telephone"),
    "linkedin_url": ("linkedin", "linked in"),
    "github": ("github", "git hub"),
    "website": ("website", "portfolio", "personal_url", "personal url", "urls[portfolio]"),
    "current_company": ("current company", "company", "org", "organization"),
    "cover_letter": ("cover", "cover letter", "message", "comments", "why are you interested"),
    "resume": ("resume", "cv", "file"),
}

_BLOCK_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("blocked_captcha", "captcha", r"captcha|recaptcha|hcaptcha|g-recaptcha|cf-turnstile"),
    ("blocked_custom_question", "login_required", r"password|sign in|log in|login|required account"),
    ("blocked_custom_question", "legal_question", r"eeoc|gender|race|ethnicity|veteran|disability|legal|conviction|criminal"),
    ("blocked_custom_question", "salary_question", r"salary|compensation|pay expectation|expected pay|desired pay"),
    ("blocked_custom_question", "visa_question", r"visa|sponsor|sponsorship|work authorization|authorized to work|right to work"),
)

_JUDGMENT_PATTERN = re.compile(
    r"why should|why do you|why are you|explain|describe|tell us|essay|short answer|"
    r"years of experience|are you willing|relocate|notice period|start date|availability",
    re.I,
)


@dataclass(frozen=True)
class FormField:
    tag: str
    attrs: dict[str, str]
    label: str = ""

    @property
    def field_id(self) -> str:
        return self.attrs.get("id") or self.attrs.get("name") or self.attrs.get("aria-label") or self.label or self.tag

    @property
    def text(self) -> str:
        bits = [
            self.attrs.get("name", ""),
            self.attrs.get("id", ""),
            self.attrs.get("placeholder", ""),
            self.attrs.get("aria-label", ""),
            self.attrs.get("autocomplete", ""),
            self.label,
            self.attrs.get("type", ""),
        ]
        return " ".join(bit for bit in bits if bit).strip()

    @property
    def required(self) -> bool:
        return "required" in self.attrs or self.attrs.get("aria-required", "").lower() == "true"


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: list[FormField] = []
        self.buttons: list[str] = []
        self._labels_by_for: dict[str, str] = {}
        self._label_stack: list[dict[str, Any]] = []
        self._body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "label":
            self._label_stack.append({"for": attr.get("for", ""), "parts": []})
            return
        if tag in {"input", "textarea", "select"}:
            input_type = attr.get("type", "").lower()
            if input_type in {"hidden", "submit", "button"}:
                return
            label = self._label_stack[-1]["parts"] if self._label_stack else []
            self.fields.append(FormField(tag=tag, attrs=attr, label=" ".join(label).strip()))
        if tag == "button":
            self._label_stack.append({"for": "__button__", "parts": []})

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_stack:
            label = self._label_stack.pop()
            if label["for"]:
                self._labels_by_for[str(label["for"])] = " ".join(label["parts"]).strip()
            elif self.fields and not self.fields[-1].label:
                last = self.fields[-1]
                self.fields[-1] = FormField(last.tag, last.attrs, " ".join(label["parts"]).strip())
        if tag == "button" and self._label_stack:
            label = self._label_stack.pop()
            self.buttons.append(" ".join(label["parts"]).strip())

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self._body_parts.append(text)
        if self._label_stack:
            self._label_stack[-1]["parts"].append(text)

    def close(self) -> None:
        super().close()
        labelled: list[FormField] = []
        for field in self.fields:
            label = field.label or self._labels_by_for.get(field.attrs.get("id", ""), "")
            labelled.append(FormField(field.tag, field.attrs, label))
        self.fields = labelled

    @property
    def body_text(self) -> str:
        return " ".join(self._body_parts)


def detect_ats(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    combined = f"{host}{path}"
    if "greenhouse.io" in combined:
        return "greenhouse"
    if "lever.co" in combined:
        return "lever"
    if "ashbyhq.com" in combined:
        return "ashby"
    return None


def _load_html(url: str, fixture_html: str | Path | None = None) -> str:
    if fixture_html:
        return Path(fixture_html).read_text(encoding="utf-8")
    parsed = urlparse(url)
    if parsed.scheme == "file":
        return Path(parsed.path).read_text(encoding="utf-8")
    if parsed.scheme in {"http", "https"}:
        req = Request(url, headers={"User-Agent": "JustHireMe apply-prep audit; no submit"})
        with urlopen(req, timeout=20) as response:  # nosec: operator-supplied URL, read-only GET
            return response.read().decode("utf-8", errors="replace")
    raise ValueError("Apply adapter needs a file, http, or https URL, or --fixture-html")


def _candidate_value(candidate: dict[str, Any], canonical: str) -> str:
    identity = candidate.get("identity") if isinstance(candidate.get("identity"), dict) else {}
    pools = [candidate, identity if isinstance(identity, dict) else {}]
    keys = {
        "first_name": ("first_name",),
        "last_name": ("last_name",),
        "name": ("name", "n", "candidate_name"),
        "email": ("email",),
        "phone": ("phone",),
        "linkedin_url": ("linkedin_url", "linkedin"),
        "github": ("github", "github_url"),
        "website": ("website", "portfolio", "portfolio_url"),
        "current_company": ("current_company", "company"),
        "cover_letter": ("cover_letter", "cover_note"),
        "resume": ("resume_path", "resume_asset", "resume"),
    }.get(canonical, (canonical,))
    if canonical in {"first_name", "last_name"}:
        name = _candidate_value(candidate, "name")
        if name and not any(str(pool.get(key) or "").strip() for pool in pools for key in keys):
            parts = name.split()
            return parts[0] if canonical == "first_name" else " ".join(parts[1:])
    for pool in pools:
        for key in keys:
            value = str(pool.get(key) or "").strip()
            if value:
                return value
    return ""


def _canonical_field(field: FormField) -> str | None:
    text = field.text.lower().replace("-", "_")
    for canonical, aliases in _FIELD_ALIASES.items():
        if any(alias in text for alias in aliases):
            return canonical
    return None


def _parse_form(html: str) -> tuple[list[FormField], str]:
    parser = _FormParser()
    parser.feed(html)
    parser.close()
    return parser.fields, parser.body_text


def prepare_apply(
    url: str,
    *,
    candidate: dict[str, Any] | None = None,
    mode: str = "dry_run",
    fixture_html: str | Path | None = None,
    audit_path: str | Path | None = None,
    allow_submit: bool = False,
) -> dict[str, Any]:
    if allow_submit:
        raise ValueError("Final submit is not implemented; whitelist support must be added in a later explicit phase.")
    candidate = candidate or {}
    ats = detect_ats(url)
    if ats not in SUPPORTED_ATS:
        result = _result(
            status="unsupported",
            ats=ats,
            url=url,
            fields_mapped=[],
            fields_skipped=[],
            block_reasons=[{"code": "unsupported", "reason": "URL is not a supported Greenhouse, Lever, or Ashby apply target."}],
            next_human_action="Open manually or add an adapter in a later phase.",
            mode=mode,
        )
        return _write_audit_if_requested(result, audit_path)

    html = _load_html(url, fixture_html)
    fields, body_text = _parse_form(html)
    block_reasons = _detect_blocks(fields, body_text)
    fields_mapped: list[dict[str, Any]] = []
    fields_skipped: list[dict[str, Any]] = []

    for field in fields:
        canonical = _canonical_field(field)
        if not canonical:
            skipped = {"field": field.field_id, "label": field.text, "reason": "unmapped_required_question" if field.required else "unmapped_optional_question"}
            fields_skipped.append(skipped)
            if field.required or _JUDGMENT_PATTERN.search(field.text):
                block_reasons.append({"code": "custom_question", "field": field.field_id, "reason": f"Custom judgment question requires human answer: {field.text}"})
            continue
        value = _candidate_value(candidate, canonical)
        if value:
            fields_mapped.append({"field": field.field_id, "type": canonical, "value_source": canonical, "filled": mode == "prepare"})
        else:
            fields_skipped.append({"field": field.field_id, "type": canonical, "label": field.text, "reason": "missing_candidate_value"})
            if field.required:
                block_reasons.append({"code": "missing_required_candidate_value", "field": field.field_id, "reason": f"Required {canonical} is missing from candidate data."})

    deduped_blocks = _dedupe_blocks(block_reasons)
    if deduped_blocks:
        status = "blocked_captcha" if any(item["code"] == "captcha" for item in deduped_blocks) else "blocked_custom_question"
        next_action = "Human must resolve the block on the ATS page; no submit or fill beyond safe mapped fields."
    elif mode == "dry_run":
        status = "applied_dry_run"
        next_action = "Review mapped/skipped fields, then rerun with --prepare if acceptable."
    else:
        status = "filled_no_submit"
        next_action = "Review the prepared ATS form in browser and manually click final submit if everything is correct."

    result = _result(
        status=status,
        ats=ats,
        url=url,
        fields_mapped=fields_mapped,
        fields_skipped=fields_skipped,
        block_reasons=deduped_blocks,
        next_human_action=next_action,
        mode=mode,
    )
    result["ready_for_manual_submit"] = status == "filled_no_submit"
    return _write_audit_if_requested(result, audit_path)


def _detect_blocks(fields: list[FormField], body_text: str) -> list[dict[str, str]]:
    haystacks = [body_text, *(field.text for field in fields)]
    blocks: list[dict[str, str]] = []
    for status, code, pattern in _BLOCK_PATTERNS:
        for text in haystacks:
            if re.search(pattern, text, re.I):
                blocks.append({"code": code if status != "blocked_captcha" else "captcha", "reason": f"Blocked by {code.replace('_', ' ')} signal: {text[:160]}"})
                break
    return blocks


def _dedupe_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for block in blocks:
        key = (str(block.get("code", "")), str(block.get("field", "")) or str(block.get("reason", "")))
        if key not in seen:
            seen.add(key)
            out.append(block)
    return out


def _result(
    *,
    status: str,
    ats: str | None,
    url: str,
    fields_mapped: list[dict[str, Any]],
    fields_skipped: list[dict[str, Any]],
    block_reasons: list[dict[str, Any]],
    next_human_action: str,
    mode: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "ats": ats or "unknown",
        "url": url,
        "mode": mode,
        "submitted": False,
        "submit_attempted": False,
        "final_submit_policy": "blocked unless a later phase adds an explicit job/ATS whitelist",
        "fields_mapped": fields_mapped,
        "fields_skipped": fields_skipped,
        "block_reasons": block_reasons,
        "next_human_action": next_human_action,
        "safe_status_outcomes": sorted(SAFE_STATUS_OUTCOMES),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_audit_if_requested(result: dict[str, Any], audit_path: str | Path | None) -> dict[str, Any]:
    if audit_path:
        path = Path(audit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        result["audit_path"] = str(path.resolve())
    return result


def build_apply_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely dry-run or prepare a supported ATS application without final submit.")
    parser.add_argument("--url", required=True, help="Greenhouse, Lever, or Ashby apply URL. file:// URLs are supported for fixtures.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Map fields and write audit only. Default.")
    mode.add_argument("--prepare", action="store_true", help="Prepare/fill safe mapped fields, then stop before final submit.")
    parser.add_argument("--candidate-json", type=Path, help="Candidate/profile JSON with name/email/phone/resume fields.")
    parser.add_argument("--fixture-html", type=Path, help="Read form HTML from a fixture instead of fetching URL.")
    parser.add_argument("--audit-path", type=Path, help="Write the audit JSON to this path.")
    parser.add_argument("--format", choices=("json",), default="json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_apply_parser().parse_args(argv)
    candidate = json.loads(args.candidate_json.read_text(encoding="utf-8")) if args.candidate_json else {}
    result = prepare_apply(
        args.url,
        candidate=candidate,
        mode="prepare" if args.prepare else "dry_run",
        fixture_html=args.fixture_html,
        audit_path=args.audit_path,
    )
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
