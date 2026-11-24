"""Pure ClinicalTrials.gov v2 handling for Step 13: URLs, parsing, dates, filters and matching.

Nothing here touches the database or the network.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

BASE_URL = "https://clinicaltrials.gov/api/v2/studies"
PAGE_SIZE = 1000
FIELDS = (
    "NCTId|BriefTitle|OverallStatus|Phase|StudyType|LeadSponsorName|PrimaryCompletionDate|"
    "CompletionDate|LastUpdatePostDate|Condition|InterventionName|InterventionType"
)
DEFAULT_THRESHOLD = 0.6
COMPLETED_MAX_AGE_DAYS = 730

_PHASE_RANK = {"EARLY_PHASE1": 1, "PHASE1": 2, "PHASE2": 3, "PHASE3": 4, "PHASE4": 5}
_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "plc",
        "co",
        "company",
        "llc",
        "lp",
        "sa",
        "ag",
        "nv",
        "bv",
        "gmbh",
        "the",
        "holdings",
        "holding",
    }  # fmt: skip
)
_NON_WORD = re.compile(r"[^a-z0-9]+")


def query_url(sponsor: str, page_token: str | None = None) -> str:
    url = (
        f"{BASE_URL}?query.spons={quote(sponsor)}&pageSize={PAGE_SIZE}&format=json"
        f"&fields={quote(FIELDS, safe='')}"
    )
    return url + (f"&pageToken={quote(page_token)}" if page_token else "")


@dataclass(frozen=True)
class ParsedDate:
    raw: str
    value: date | None
    precision: str  # "day", "month" or ""


def parse_ct_date(raw: str | None) -> ParsedDate:
    """YYYY-MM-DD is a day; YYYY-MM is stored as the 1st with precision month."""
    text = (raw or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return ParsedDate(text, date.fromisoformat(text), "day")
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return ParsedDate(text, date.fromisoformat(f"{text}-01"), "month")
    return ParsedDate(text, None, "")


def highest_phase(phases: list[str]) -> str:
    """The highest phase of a (possibly multi-phase) study, e.g. PHASE2/PHASE3 gives PHASE3."""
    ranked = [p for p in phases if p in _PHASE_RANK]
    return max(ranked, key=_PHASE_RANK.__getitem__) if ranked else ("NA" if phases else "")


@dataclass(frozen=True)
class ParsedStudy:
    nct_id: str
    sponsor_name: str
    status: str
    study_type: str
    phases: list[str]
    highest_phase: str
    primary_completion: ParsedDate
    completion: ParsedDate
    last_update_posted: date | None
    title: str
    conditions: list[str] = field(default_factory=list)
    interventions: list[dict[str, str]] = field(default_factory=list)

    def content(self) -> dict[str, Any]:
        """Everything that, when changed, makes a new version."""
        return {
            "sponsor_name": self.sponsor_name,
            "status": self.status,
            "study_type": self.study_type,
            "phases": self.phases,
            "primary_completion": self.primary_completion.raw,
            "completion": self.completion.raw,
            "last_update_posted": self.last_update_posted.isoformat()
            if self.last_update_posted
            else "",
            "title": self.title,
            "conditions": self.conditions,
            "interventions": self.interventions,
        }


def parse_studies(content: bytes) -> tuple[list[ParsedStudy], str | None]:
    """The studies of one page and the token for the next page (None on the last)."""
    data = json.loads(content)
    studies = []
    for item in data.get("studies", []):
        p = item["protocolSection"]
        status = p.get("statusModule", {})
        design = p.get("designModule", {})
        phases = list(design.get("phases", []))
        posted = (status.get("lastUpdatePostDateStruct") or {}).get("date")
        studies.append(
            ParsedStudy(
                nct_id=p["identificationModule"]["nctId"],
                sponsor_name=(p.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get(
                    "name", ""
                ),
                status=status.get("overallStatus", ""),
                study_type=design.get("studyType", ""),
                phases=phases,
                highest_phase=highest_phase(phases),
                primary_completion=parse_ct_date(
                    (status.get("primaryCompletionDateStruct") or {}).get("date")
                ),
                completion=parse_ct_date((status.get("completionDateStruct") or {}).get("date")),
                last_update_posted=parse_ct_date(posted).value,
                title=p["identificationModule"].get("briefTitle", ""),
                conditions=list(p.get("conditionsModule", {}).get("conditions", [])),
                interventions=[
                    {"type": i.get("type", ""), "name": i.get("name", "")}
                    for i in p.get("armsInterventionsModule", {}).get("interventions", [])
                ],
            )
        )
    return studies, data.get("nextPageToken")


def keep_study(study: ParsedStudy, today: date) -> tuple[bool, str]:
    """Observational studies and long-completed studies are dropped; every other status stays."""
    if study.study_type == "OBSERVATIONAL":
        return False, "observational"
    if study.status == "COMPLETED":
        done = study.completion.value or study.primary_completion.value
        if done is not None and done < today - timedelta(days=COMPLETED_MAX_AGE_DAYS):
            return False, "completed_over_730_days_ago"
    return True, ""


def _tokens(name: str) -> list[str]:
    words = [w for w in _NON_WORD.split(name.lower()) if w and w not in _SUFFIXES]
    return sorted(words)


def similarity(a: str, b: str) -> float:
    """0 to 1. Token-based: company suffixes are ignored and word order does not matter."""
    left, right = " ".join(_tokens(a)), " ".join(_tokens(b))
    if not left or not right:
        return 0.0
    return round(difflib.SequenceMatcher(None, left, right).ratio(), 4)


def match_sponsor(
    sponsor_name: str,
    aliases: list[tuple[str, bool]],
    threshold: float = DEFAULT_THRESHOLD,
) -> float | None:
    """The best score of ``sponsor_name`` against ``(alias, is_override)`` pairs, or None.

    A manual override with the same name always matches with score 1. Other aliases must reach
    the threshold.
    """
    best: float | None = None
    for alias, override in aliases:
        if override and alias.strip().lower() == sponsor_name.strip().lower():
            return 1.0
        score = similarity(sponsor_name, alias)
        if score >= threshold and (best is None or score > best):
            best = score
    return best


def derive_alias(company_name: str) -> str:
    """The name searched for when a company has no alias: its name without a trailing suffix."""
    words = company_name.replace(",", " ").split()
    while words and _NON_WORD.sub("", words[-1].lower()) in _SUFFIXES:
        words.pop()
    return " ".join(words) or company_name
