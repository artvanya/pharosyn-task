import json
import requests

BASE_URL = "https://clinicaltrials.gov/api/v2"
TIMEOUT = 10
MAX_RETRIES = 2


class ClinicalTrialsClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, params: dict) -> dict:
        url = f"{BASE_URL}{path}"
        last_err = None
        for _ in range(MAX_RETRIES):
            try:
                r = self.session.get(url, params=params, timeout=TIMEOUT)
                r.raise_for_status()
                return r.json()
            except requests.exceptions.Timeout:
                last_err = "Request timed out after 10 s"
            except requests.exceptions.HTTPError as e:
                code = e.response.status_code
                body = e.response.text[:200]
                # 4xx errors won't change on retry
                if 400 <= code < 500:
                    return {"error": f"HTTP {code}: {body}"}
                last_err = f"HTTP {code}: {body}"
            except requests.exceptions.RequestException as e:
                last_err = str(e)
        return {"error": last_err}

    # ── public API ────────────────────────────────────────────────────────────

    def search_trials(
        self,
        condition: str = None,
        status: str | list[str] = None,
        intervention: str = None,
        term: str = None,
        page_size: int = 10,
        page_token: str = None,
        fields: list[str] = None,
        sort: list[str] = None,
        count_total: bool = False,
    ) -> dict:
        params: dict = {"format": "json", "pageSize": min(page_size, 50)}
        if condition:
            params["query.cond"] = condition
        if term:
            params["query.term"] = term
        if intervention:
            params["query.intr"] = intervention
        if status:
            statuses = status if isinstance(status, list) else [status]
            params["filter.overallStatus"] = "|".join(statuses)
        if page_token:
            params["pageToken"] = page_token
        if fields:
            params["fields"] = "|".join(fields)
        if sort:
            params["sort"] = "|".join(sort)
        if count_total:
            params["countTotal"] = "true"
        return self._get("/studies", params)

    def get_study(self, nct_id: str) -> dict:
        """Fetch the full record for a single trial by NCT ID."""
        return self._get(f"/studies/{nct_id}", {"format": "json"})

    # ── normalisation ─────────────────────────────────────────────────────────

    def normalize_study(self, raw_study: dict) -> dict:
        protocol = raw_study.get("protocolSection", {})

        id_mod = protocol.get("identificationModule", {})
        status_mod = protocol.get("statusModule", {})
        design_mod = protocol.get("designModule", {})
        conditions_mod = protocol.get("conditionsModule", {})
        description_mod = protocol.get("descriptionModule", {})
        sponsor_mod = protocol.get("sponsorCollaboratorsModule", {})
        outcomes_mod = protocol.get("outcomesModule", {})
        eligibility_mod = protocol.get("eligibilityModule", {})
        arms_mod = protocol.get("armsInterventionsModule", {})

        primary_outcomes = [
            o.get("measure") for o in outcomes_mod.get("primaryOutcomes", []) if o.get("measure")
        ]
        interventions = [
            i.get("name") for i in arms_mod.get("interventions", []) if i.get("name")
        ]
        lead_sponsor = sponsor_mod.get("leadSponsor", {}).get("name")
        collaborators = [c.get("name") for c in sponsor_mod.get("collaborators", []) if c.get("name")]

        nct_id = id_mod.get("nctId")
        return {
            "nct_id": nct_id,
            "title": id_mod.get("briefTitle"),
            "status": status_mod.get("overallStatus"),
            "phase": design_mod.get("phases", []),
            "conditions": conditions_mod.get("conditions", []),
            "summary": description_mod.get("briefSummary"),
            "lead_sponsor": lead_sponsor,
            "collaborators": collaborators,
            "interventions": interventions,
            "primary_outcomes": primary_outcomes,
            "min_age": eligibility_mod.get("minimumAge"),
            "max_age": eligibility_mod.get("maximumAge"),
            "sex": eligibility_mod.get("sex"),
            "ctgov_url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else None,
        }

    def normalize_search_results(self, raw: dict) -> dict:
        """Normalize a full /studies page response. Returns studies list + metadata."""
        if "error" in raw:
            return raw
        studies = [self.normalize_study(s) for s in raw.get("studies", [])]
        return {
            "studies": studies,
            "total_count": raw.get("totalCount"),
            "next_page_token": raw.get("nextPageToken"),
        }


if __name__ == "__main__":
    client = ClinicalTrialsClient()
    result = client.search_trials(condition="obesity", status="RECRUITING")
    normalized = client.normalize_search_results(result)
    print(f"Returned {len(normalized['studies'])} studies\n")
    for t in normalized["studies"][:3]:
        print(json.dumps(t, indent=2))
