"""Local, credential-free job ledger for visible website video generation."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from backend.services.family_providers import build_provider_prompt
from backend.services.family_shots import ContinuationFromExtractedFrameJoin, FamilyShotPlan
from backend.services.family_takes import FamilyTakeExecutor, OperationLedger
from backend.services.web_provider_adapters import ADAPTERS, evaluate_observation


class WebGatewayError(ValueError):
    """A web job cannot be prepared or advanced safely."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(b"family-web-job/v1\0" + _canonical(value)).hexdigest()


def _cost(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != {"amount", "unit"}:
        raise WebGatewayError("cost needs amount and unit")
    try:
        amount = Decimal(str(value["amount"]))
    except (InvalidOperation, ValueError) as exc:
        raise WebGatewayError("cost amount is invalid") from exc
    if not amount.is_finite() or amount < 0:
        raise WebGatewayError("cost amount must be finite and nonnegative")
    unit = value["unit"]
    if not isinstance(unit, str) or not unit or len(unit) > 40:
        raise WebGatewayError("cost unit is invalid")
    return {"amount": format(amount.normalize(), "f"), "unit": unit}


def _public_material(value: Any) -> None:
    forbidden = ("password", "cookie", "token", "secret", "credential", "api_key", "auth")
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).lower().replace("-", "_")
            if any(word in name for word in forbidden):
                raise WebGatewayError("secret-like field cannot be stored")
            _public_material(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _public_material(child)
    elif isinstance(value, str) and (value.startswith("data:") or "?token=" in value.lower()):
        raise WebGatewayError("secret-like value cannot be stored")


@dataclass(frozen=True)
class WebJob:
    job_id: str
    plan_sha256: str
    state: str
    request: dict[str, Any]
    observation: dict[str, Any] | None = None
    approval: dict[str, Any] | None = None
    website_job_id: str | None = None
    take_id: str | None = None


class WebProviderGateway:
    def __init__(self, project_root: Path) -> None:
        self.ledger = OperationLedger(project_root)
        self.project_root = self.ledger.project_root
        with closing(self.ledger._connect()) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS web_provider_jobs (
                    job_id TEXT PRIMARY KEY,
                    plan_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    observation_json TEXT,
                    approval_json TEXT,
                    website_job_id TEXT,
                    take_id TEXT
                )
            """)

    @staticmethod
    def _from_row(row: Any) -> WebJob:
        return WebJob(
            job_id=row["job_id"], plan_sha256=row["plan_sha256"], state=row["state"],
            request=json.loads(row["request_json"]),
            observation=json.loads(row["observation_json"]) if row["observation_json"] else None,
            approval=json.loads(row["approval_json"]) if row["approval_json"] else None,
            website_job_id=row["website_job_id"], take_id=row["take_id"],
        )

    def get_job(self, job_id: str) -> WebJob:
        with closing(self.ledger._connect()) as connection:
            row = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown web job: {job_id}")
        return self._from_row(row)

    def stage_manifest(self, job_id: str, prepared_by_source: Mapping[str, Path]) -> list[dict[str, str]]:
        """Return only the exact, unchanged media inputs a browser driver may stage."""
        job = self.get_job(job_id)
        source_ids = job.request["source_ids"]
        if set(prepared_by_source) != set(source_ids):
            raise WebGatewayError("staging paths must match the requested sources exactly")
        result: list[dict[str, str]] = []
        for source_id in source_ids:
            path = Path(prepared_by_source[source_id])
            if path.is_symlink() or getattr(path, "is_junction", lambda: False)() or not path.is_file():
                raise WebGatewayError(f"staging source is missing or linked: {source_id}")
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            if sha256 != job.request["source_sha256"][source_id]:
                raise WebGatewayError(f"staging source changed since preparation: {source_id}")
            result.append({
                "role": job.request["source_roles"][source_id],
                "source_id": source_id,
                "path": str(path.resolve()),
                "sha256": sha256,
            })
        return result

    def prepare(
        self, plan: FamilyShotPlan, shot_id: str, prepared_by_source: Mapping[str, Path], *,
        website: str, requested_model: str, controls: Mapping[str, Any],
        expected_cost: Mapping[str, Any] | None = None,
        free_credits_only: bool = False,
    ) -> WebJob:
        if not isinstance(plan, FamilyShotPlan):
            raise WebGatewayError("plan must be a validated FamilyShotPlan")
        shot = next((item for item in plan.shots if item.shot_id == shot_id), None)
        if shot is None:
            raise WebGatewayError("shot is absent from plan")
        if any(
            isinstance(join, ContinuationFromExtractedFrameJoin)
            and join.incoming_shot_id == shot_id
            for join in plan.joins or ()
        ):
            raise WebGatewayError(
                "extracted-frame continuation requires an accepted predecessor binding; manual gateway preparation is blocked"
            )
        if website not in ADAPTERS:
            raise WebGatewayError("website is unsupported")
        if not isinstance(requested_model, str) or not requested_model.strip() or len(requested_model) > 120:
            raise WebGatewayError("requested model is invalid")
        if not isinstance(free_credits_only, bool):
            raise WebGatewayError("free_credits_only must be boolean")
        source_sha256: dict[str, str] = {}
        roles: dict[str, str] = {}
        for item in shot.sources:
            path = Path(prepared_by_source[item.source_id])
            if path.is_symlink() or not path.is_file():
                raise WebGatewayError(f"prepared source is missing or linked: {item.source_id}")
            source_sha256[item.source_id] = hashlib.sha256(path.read_bytes()).hexdigest()
            roles[item.role] = item.source_id
        mandatory: dict[str, Any] = {"duration_seconds": shot.timing.generation_duration_seconds}
        if "start" in roles:
            mandatory["start_frame"] = roles["start"]
        if "end" in roles:
            mandatory["end_frame"] = roles["end"]
        if shot.character_elements:
            mandatory["character_references"] = [
                {"person_id": element.person_id, "frontal_source_id": element.frontal_source_id,
                 "supporting_source_ids": list(element.supporting_reference_source_ids)}
                for element in shot.character_elements
            ]
        overlap = set(mandatory) & set(controls)
        if overlap:
            raise WebGatewayError(f"derived controls cannot be overridden: {sorted(overlap)}")
        _public_material(controls)
        request = {
            "schema_version": "family-web-job/v1", "shot_id": shot_id,
            "website": website, "requested_model": requested_model.strip(),
            "source_ids": [item.source_id for item in shot.sources],
            "source_roles": {item.source_id: item.role for item in shot.sources},
            "source_sha256": source_sha256,
            "prompt": build_provider_prompt(shot),
            "controls": {**mandatory, **dict(controls)},
            "expected_cost": _cost(expected_cost) if expected_cost is not None else None,
            "free_credits_only": free_credits_only,
        }
        try:
            encoded = _canonical(request)
        except (TypeError, ValueError) as exc:
            raise WebGatewayError("controls must be JSON values") from exc
        plan_sha256 = hashlib.sha256(_canonical(plan.model_dump(mode="json"))).hexdigest()
        request["plan_sha256"] = plan_sha256
        job_id = "web_" + _digest(request)[:32]
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO web_provider_jobs(job_id, plan_sha256, state, request_json) VALUES (?, ?, 'prepared', ?)",
                    (job_id, plan_sha256, _canonical(request).decode("utf-8")),
                )
            elif existing["request_json"] != _canonical(request).decode("utf-8"):
                raise WebGatewayError("job fingerprint collision")
            connection.commit()
        return self.get_job(job_id)

    def observe(self, job_id: str, observation: Mapping[str, Any]) -> WebJob:
        allowed = {
            "calibration_id", "displayed_model", "supported_controls", "selected_controls",
            "displayed_cost", "evidence_ref", "login_required", "free_credit_confirmed",
            "free_credit_evidence_ref", "captcha_required", "terms_required",
            "displayed_free_balance",
        }
        if set(observation) - allowed:
            raise WebGatewayError("observation contains unsupported fields")
        observed = dict(observation)
        _public_material(observed)
        for field in ("evidence_ref", "free_credit_evidence_ref"):
            if observed.get(field) is not None:
                try:
                    observed[field] = self._evidence(observed[field])
                except WebGatewayError as exc:
                    raise WebGatewayError(f"{field} is invalid evidence") from exc
        if observed.get("displayed_cost") is not None:
            observed["displayed_cost"] = _cost(observed["displayed_cost"])
        if observed.get("displayed_free_balance") is not None:
            observed["displayed_free_balance"] = _cost(observed["displayed_free_balance"])
        encoded = _canonical(observed)
        if len(encoded) > 20_000:
            raise WebGatewayError("observation is too large")
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown web job: {job_id}")
            job = self._from_row(row)
            if job.state not in {"prepared", "needs_login", "needs_human", "ready"}:
                raise WebGatewayError("cannot replace an observation after submission started")
            result = evaluate_observation(job.request["website"], job.request, observed)
            state = "needs_login" if result == "needs_login" else "ready" if result == "ready" else "needs_human"
            digest = hashlib.sha256(b"family-web-observation/v1\0" + encoded).hexdigest()
            stored = {**observed, "result": result, "sha256": digest}
            connection.execute(
                "UPDATE web_provider_jobs SET state = ?, observation_json = ?, approval_json = NULL WHERE job_id = ?",
                (state, _canonical(stored).decode("utf-8"), job_id),
            )
            connection.commit()
        return self.get_job(job_id)

    def approve(self, job_id: str, observation_sha256: str, *, approved_by: str) -> WebJob:
        if not isinstance(approved_by, str) or not approved_by.strip() or len(approved_by) > 120:
            raise WebGatewayError("approver name is invalid")
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown web job: {job_id}")
            job = self._from_row(row)
            if job.state != "ready" or job.observation is None:
                raise WebGatewayError("job is not ready for approval")
            if job.observation["sha256"] != observation_sha256:
                raise WebGatewayError("observation changed since approval was requested")
            material = {
                "job_id": job_id, "job_fingerprint": job_id[4:],
                "observation_sha256": observation_sha256,
                "displayed_model": job.observation["displayed_model"],
                "selected_controls": job.observation["selected_controls"],
                "displayed_cost": job.observation["displayed_cost"],
                "approved_by": approved_by.strip(),
            }
            material["sha256"] = hashlib.sha256(
                b"family-web-approval/v1\0" + _canonical(material)
            ).hexdigest()
            connection.execute(
                "UPDATE web_provider_jobs SET approval_json = ? WHERE job_id = ?",
                (_canonical(material).decode("utf-8"), job_id),
            )
            connection.commit()
        return self.get_job(job_id)

    def mark_submission_started(self, job_id: str, visible_recheck: Mapping[str, Any]) -> WebJob:
        if not isinstance(visible_recheck, Mapping):
            raise WebGatewayError("fresh visible recheck is required")
        _public_material(visible_recheck)
        self._evidence(visible_recheck.get("evidence_ref"))
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown web job: {job_id}")
            job = self._from_row(row)
            if job.state != "ready":
                raise WebGatewayError("job is not ready for submission")
            if job.approval is None:
                raise WebGatewayError("exact approval is required")
            if job.observation is None or job.approval["observation_sha256"] != job.observation["sha256"]:
                raise WebGatewayError("approval differs from visible observation")
            recheck = dict(visible_recheck)
            if recheck.get("displayed_cost") is not None:
                recheck["displayed_cost"] = _cost(recheck["displayed_cost"])
            if recheck.get("displayed_free_balance") is not None:
                recheck["displayed_free_balance"] = _cost(recheck["displayed_free_balance"])
            if evaluate_observation(job.request["website"], job.request, recheck) != "ready":
                raise WebGatewayError("visible recheck differs from approved settings or cost")
            if any(recheck.get(key) != job.approval.get(approved_key) for key, approved_key in (
                ("displayed_model", "displayed_model"),
                ("selected_controls", "selected_controls"),
                ("displayed_cost", "displayed_cost"),
            )):
                raise WebGatewayError("visible recheck differs from exact approval")
            observation = {**job.observation, "submission_recheck": recheck}
            connection.execute(
                "UPDATE web_provider_jobs SET state = 'unknown', observation_json = ? WHERE job_id = ?",
                (_canonical(observation).decode("utf-8"), job_id),
            )
            connection.commit()
        return self.get_job(job_id)

    @staticmethod
    def _evidence(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 240:
            raise WebGatewayError("unknown outcome requires visible history evidence")
        if "?" in value or "#" in value or "\\" in value:
            raise WebGatewayError("evidence must be an opaque reference without URL query or path")
        return value.strip()

    def _transition_with_history(
        self, job_id: str, *, from_states: set[str], state: str,
        evidence_ref: str, website_job_id: str | None = None,
        clear_approval: bool = False, zero_charge_confirmed: bool = False,
    ) -> WebJob:
        evidence = self._evidence(evidence_ref)
        if website_job_id is not None and (
            not isinstance(website_job_id, str) or not website_job_id.strip()
            or len(website_job_id) > 160 or "?" in website_job_id or "#" in website_job_id
        ):
            raise WebGatewayError("website job ID is invalid")
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown web job: {job_id}")
            job = self._from_row(row)
            if job.state not in from_states:
                raise WebGatewayError(f"history resolution requires {', '.join(sorted(from_states))} state")
            if state in {"submitted", "running"} and not (website_job_id or job.website_job_id):
                raise WebGatewayError("website job ID is required")
            previous = job.observation or {}
            history = [*previous.get("history_evidence", []), {
                "state": state, "evidence_ref": evidence,
                "zero_charge_confirmed": zero_charge_confirmed,
            }]
            observation = {**previous, "history_evidence": history}
            connection.execute(
                "UPDATE web_provider_jobs SET state = ?, observation_json = ?, approval_json = ?, website_job_id = ? WHERE job_id = ?",
                (state, _canonical(observation).decode("utf-8"),
                 None if clear_approval else row["approval_json"], website_job_id or job.website_job_id, job_id),
            )
            connection.commit()
        return self.get_job(job_id)

    def record_submission(self, job_id: str, website_job_id: str, *, evidence_ref: str) -> WebJob:
        return self._transition_with_history(
            job_id, from_states={"unknown"}, state="submitted",
            evidence_ref=evidence_ref, website_job_id=website_job_id,
        )

    def record_history_resolution(
        self, job_id: str, outcome: str, *, evidence_ref: str,
        website_job_id: str | None = None, zero_charge_confirmed: bool = False,
    ) -> WebJob:
        if outcome not in {"found", "running", "absent", "uncertain"}:
            raise WebGatewayError("history outcome is invalid")
        if outcome == "absent" and zero_charge_confirmed is not True:
            raise WebGatewayError("confirmed absence requires visible zero charge evidence")
        target = {"found": "submitted", "running": "running", "absent": "ready", "uncertain": "unknown"}[outcome]
        return self._transition_with_history(
            job_id, from_states={"unknown"}, state=target,
            evidence_ref=evidence_ref, website_job_id=website_job_id,
            clear_approval=outcome == "absent",
            zero_charge_confirmed=zero_charge_confirmed,
        )

    def record_running(self, job_id: str, *, evidence_ref: str) -> WebJob:
        return self._transition_with_history(
            job_id, from_states={"submitted", "running"}, state="running",
            evidence_ref=evidence_ref,
        )

    def record_failed(self, job_id: str, *, evidence_ref: str) -> WebJob:
        return self._transition_with_history(
            job_id, from_states={"submitted", "running"}, state="failed",
            evidence_ref=evidence_ref,
        )

    def record_recovered_running(self, job_id: str, *, evidence_ref: str) -> WebJob:
        """Recover a false failure after the website exposes the same job again."""
        return self._transition_with_history(
            job_id, from_states={"failed"}, state="running",
            evidence_ref=evidence_ref,
        )

    def import_result(
        self, job_id: str, plan: FamilyShotPlan, prepared_by_source: Mapping[str, Path],
        video_path: Path, *, evidence_ref: str,
    ) -> WebJob:
        evidence = self._evidence(evidence_ref)
        job = self.get_job(job_id)
        if job.state not in {"submitted", "running", "completed"} or not job.website_job_id:
            raise WebGatewayError("a visible website job must be identified before import")
        if job.take_id is not None:
            with closing(self.ledger._connect()) as connection:
                saved = connection.execute("SELECT request_json, output_sha256 FROM takes WHERE take_id = ?", (job.take_id,)).fetchone()
            if saved is None:
                raise WebGatewayError("completed web job take is missing")
            prior = json.loads(saved["request_json"])
            if prior.get("download_evidence_ref") != evidence:
                raise WebGatewayError("completed web job has different import evidence")
            candidate = Path(video_path)
            if candidate.is_symlink() or not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != saved["output_sha256"]:
                raise WebGatewayError("completed web job has different output bytes")
        current_plan_hash = hashlib.sha256(_canonical(plan.model_dump(mode="json"))).hexdigest()
        if current_plan_hash != job.plan_sha256:
            raise WebGatewayError("plan changed since job preparation")
        for source_id, digest in job.request["source_sha256"].items():
            path = Path(prepared_by_source[source_id])
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise WebGatewayError(f"prepared source changed since job preparation: {source_id}")
        executor = FamilyTakeExecutor(self.project_root, None)
        record = executor.import_external_take(
            job_id=job_id, request=job.request, website_job_id=job.website_job_id,
            observation=job.observation or {}, video_path=video_path, evidence_ref=evidence,
        )
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM web_provider_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row["take_id"] is not None and row["take_id"] != record.take_id:
                raise WebGatewayError("web job already has a different imported take")
            connection.execute(
                "UPDATE web_provider_jobs SET state = 'completed', take_id = ? WHERE job_id = ?",
                (record.take_id, job_id),
            )
            connection.commit()
        return self.get_job(job_id)
