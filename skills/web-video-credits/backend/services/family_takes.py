"""Durable, budgeted execution of family-film provider requests.

Public integration points:

* :func:`dry_run_family_plan` validates every shot, source payload, and dated
  price without reserving money or contacting a provider.
* :class:`OperationLedger` provides generic bounded reservations for video
  generation and later review operations.
* :class:`FamilyTakeExecutor` persists an immutable take before submission,
  resumes submitted jobs, and atomically publishes unique output files.

The SQLite ledger lives at ``<project>/metadata/family_takes.sqlite3``.  It
stores compact request material but never credentials or base64 image payloads.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

import httpx

from backend.services.family_providers import ProviderRequest, build_provider_request
from backend.services.family_shots import FamilyShot, FamilyShotPlan
from backend.services.video_pricing import estimate_video_cost
from backend.services.project_schema import CLIPS_DIRNAME, CLIPS_RAW_DIRNAME


MICRO_USD_PER_USD = 1_000_000
DEFAULT_LEDGER_NAME = "family_takes.sqlite3"
MAX_TAKES_PER_SHOT_ROUTE = 2
PROVIDER_EXECUTION_RECEIPT_SCHEMA = "family-provider-execution-receipt/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_PROVIDER_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")
_REVIEW_OPERATION_KINDS = frozenset({
    "gemini_review",
    "family_continuity_review",
    "family_gate_review",
    "family_film_review_full",
    "family_film_review_second_look",
})
_RECEIPT_OPERATION_STATES = frozenset({"started", "unknown", "completed"})


class FamilyTakeError(RuntimeError):
    """Base error for durable take execution."""


class PaidAuthorizationError(FamilyTakeError):
    """A paid route was requested without explicit authorization and budget."""


class PriceUnavailableError(FamilyTakeError):
    """Current price evidence is missing or expired."""


class BudgetExceededError(FamilyTakeError):
    """A bounded reservation would exceed the persisted project budget."""


class TakeLimitError(FamilyTakeError):
    """A shot/route already has the maximum number of immutable takes."""


class OperationStateError(FamilyTakeError):
    """An operation state transition or idempotency check failed."""


class OutputIntegrityError(FamilyTakeError):
    """An immutable output is missing, replaced, or does not match its hash."""


class UnsafeProviderUrlError(FamilyTakeError):
    """A credentialed queue URL is redirected, malformed, or foreign."""


@dataclass(frozen=True)
class QueueSubmission:
    job_id: str
    status_url: str
    result_url: str
    status: str = "QUEUED"


@dataclass(frozen=True)
class QueueStatus:
    status: str
    detail: str | None = None


@dataclass(frozen=True)
class QueueResult:
    output_url: str
    actual_usage_usd: Decimal | None = None


class QueueTransport(Protocol):
    """Injectable queue boundary. Implementations must not retry requests."""

    def submit(self, route: str, payload: Mapping[str, Any], credential: str) -> QueueSubmission: ...

    def poll(self, status_url: str, credential: str) -> QueueStatus: ...

    def fetch_result(self, result_url: str, credential: str) -> QueueResult: ...

    def download(self, output_url: str) -> bytes: ...


class LocalRenderer(Protocol):
    """Optional local still-hold renderer injected by the later integration."""

    def __call__(self, request: ProviderRequest) -> bytes: ...


@dataclass(frozen=True)
class PlannedTake:
    request: ProviderRequest
    upper_bound_micro_usd: int
    price_quote: Mapping[str, Any]


@dataclass(frozen=True)
class FamilyDryRunPlan:
    shots: tuple[PlannedTake, ...]
    total_upper_bound_micro_usd: int

    @property
    def total_upper_bound_usd(self) -> Decimal:
        return Decimal(self.total_upper_bound_micro_usd) / MICRO_USD_PER_USD


@dataclass(frozen=True)
class OperationReservation:
    operation_id: str
    kind: str
    request_state_hash: str
    status: str
    upper_bound_micro_usd: int
    actual_usage_micro_usd: int | None


@dataclass(frozen=True)
class ProviderExecutionReceipt:
    """Immutable proof that one review operation received one provider response."""

    operation_id: str
    request_sha256: str
    response_sha256: str
    interaction_id: str
    provider_model: str
    receipt_sha256: str
    created_at: str


@dataclass(frozen=True)
class BudgetAuthorization:
    old_total_micro_usd: int
    new_total_micro_usd: int
    held_micro_usd: int
    changed: bool


@dataclass(frozen=True)
class TakeRecord:
    take_id: str
    shot_id: str
    route: str
    request_hash: str
    status: str
    provider_job_id: str | None
    provider_status: str | None
    status_url: str | None
    result_url: str | None
    output_url: str | None
    output_path: Path
    output_sha256: str | None
    result_actual_micro_usd: int | None
    upper_bound_micro_usd: int
    actual_usage_micro_usd: int | None
    error: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _usd_to_micro_usd(value: Decimal | str | int | float) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("money must be a finite decimal USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError("money must be a finite non-negative USD amount")
    units = amount * MICRO_USD_PER_USD
    if units != units.to_integral_value():
        raise ValueError("money supports at most six decimal places")
    if units > 9_000_000_000_000_000_000:
        raise ValueError("money amount exceeds SQLite integer capacity")
    return int(units)


def _validate_state_hash(request_state_hash: str) -> None:
    if not _SHA256_RE.fullmatch(request_state_hash):
        raise ValueError("request_state_hash must be a lowercase SHA-256 hex digest")


def _validated_sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _validated_provider_value(value: str, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not _SAFE_PROVIDER_VALUE_RE.fullmatch(value)
        or ".." in value
    ):
        raise ValueError(f"{name} is not a safe bounded provider identifier")
    return value


def _validated_receipt_created_at(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise OperationStateError("provider receipt created_at is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise OperationStateError("provider receipt created_at is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timezone.utc.utcoffset(None)
        or parsed.isoformat() != value
    ):
        raise OperationStateError("provider receipt created_at is not canonical UTC")
    return value


def provider_execution_receipt_sha256(
    *,
    operation_id: str,
    request_sha256: str,
    response_sha256: str,
    interaction_id: str,
    provider_model: str,
    created_at: str,
) -> str:
    """Hash every immutable receipt field using canonical JSON and domain separation."""
    if not isinstance(operation_id, str) or not _SAFE_ID_RE.fullmatch(operation_id):
        raise ValueError("operation_id must be a short stable identifier")
    request_digest = _validated_sha256(request_sha256, "request_sha256")
    response_digest = _validated_sha256(response_sha256, "response_sha256")
    interaction = _validated_provider_value(interaction_id, "interaction_id", maximum=256)
    model = _validated_provider_value(provider_model, "provider_model", maximum=128)
    timestamp = _validated_receipt_created_at(created_at)
    canonical = json.dumps(
        {
            "created_at": timestamp,
            "interaction_id": interaction,
            "operation_id": operation_id,
            "provider_model": model,
            "request_sha256": request_digest,
            "response_sha256": response_digest,
            "schema_version": PROVIDER_EXECUTION_RECEIPT_SCHEMA,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _assert_safe_stored_material(value: Any, path: str = "request_material") -> None:
    """Reject credential-like fields and inline base64 before durable storage."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower().replace("-", "_")
            if (
                key_text in {"authorization", "credential", "credentials", "api_key", "fal_key", "secret", "access_token"}
                or key_text.endswith("_api_key")
            ):
                raise ValueError(f"credential-like field cannot be stored: {path}.{key}")
            _assert_safe_stored_material(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_stored_material(child, f"{path}[{index}]")
    elif isinstance(value, str) and value.startswith("data:") and ";base64," in value[:100]:
        raise ValueError(f"base64 data URI cannot be stored: {path}")


def _json_text(value: Mapping[str, Any]) -> str:
    _assert_safe_stored_material(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_error_code(stage: str, error: BaseException) -> str:
    """Return a bounded code without serializing exception text or response bodies."""
    class_name = re.sub(r"[^A-Za-z0-9_]", "_", type(error).__name__)[:80] or "Exception"
    return f"{stage}:{class_name}"


def _operation_id(kind: str, request_state_hash: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{request_state_hash}".encode("utf-8")).hexdigest()
    return f"op_{digest[:32]}"


def operation_id_for(kind: str, request_state_hash: str) -> str:
    """Return the stable public operation ID for exact kind/state evidence."""
    if not isinstance(kind, str) or not _SAFE_ID_RE.fullmatch(kind):
        raise ValueError("kind must be a short stable identifier")
    _validate_state_hash(request_state_hash)
    return _operation_id(kind, request_state_hash)


def _take_id(request_hash: str) -> str:
    return f"take_{request_hash[:32]}"


def _route_slug(route: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", route).strip("-")[:64]


def _prepared_mapping(prepared: Any) -> Mapping[str, Any]:
    return getattr(prepared, "prepared_by_source", prepared)


def dry_run_family_plan(
    plan: FamilyShotPlan,
    prepared_by_source: Mapping[str, Any] | Any,
    *,
    today: date | None = None,
    image_url_encoder: Callable[[bytes, str], str] | None = None,
) -> FamilyDryRunPlan:
    """Validate every shot, source payload, and current price without dispatch.

    The only local I/O is reading source bytes for request hashing.  No budget
    row is changed and no transport method is called.
    """
    prepared = _prepared_mapping(prepared_by_source)
    planned: list[PlannedTake] = []
    joins = tuple(plan.joins or ())
    for index, shot in enumerate(plan.shots):
        kwargs = {} if image_url_encoder is None else {"image_url_encoder": image_url_encoder}
        incoming_join = joins[index - 1] if index > 0 and joins else None
        outgoing_join = joins[index] if index < len(joins) else None
        request = build_provider_request(
            shot,
            prepared,
            incoming_join=incoming_join,
            outgoing_join=outgoing_join,
            **kwargs,
        )
        if request.capability.execution == "local":
            quote: Mapping[str, Any] = {
                "status": "local",
                "currency": "USD",
                "model": request.route,
                "base_usd": 0,
                "actual_invoice": False,
                "video_only": True,
                "audio": False,
            }
            upper_bound = 0
        else:
            quote = estimate_video_cost([request.duration_seconds], request.route, today=today)
            if quote.get("status") != "estimate" or quote.get("base_usd") is None:
                raise PriceUnavailableError(str(quote.get("reason", "price unavailable")))
            upper_bound = _usd_to_micro_usd(quote["base_usd"])
            if upper_bound <= 0:
                raise PriceUnavailableError("paid provider price must be nonzero")
        planned.append(PlannedTake(request=request, upper_bound_micro_usd=upper_bound, price_quote=quote))
    return FamilyDryRunPlan(
        shots=tuple(planned),
        total_upper_bound_micro_usd=sum(item.upper_bound_micro_usd for item in planned),
    )


class OperationLedger:
    """SQLite-backed bounded reservations reusable by generation and review."""

    def __init__(self, project_root: Path, *, total_budget_usd: Decimal | str | int | float = 0) -> None:
        self.project_root = Path(project_root).resolve()
        if not self.project_root.is_dir():
            raise ValueError(f"project_root is not a directory: {project_root}")
        metadata = self.project_root / "metadata"
        if metadata.exists() and (metadata.is_symlink() or not metadata.is_dir()):
            raise ValueError("project metadata path must be a real directory")
        metadata.mkdir(exist_ok=True)
        if metadata.is_symlink():
            raise ValueError("project metadata path must not be a symlink")
        self.db_path = metadata / DEFAULT_LEDGER_NAME
        if self.db_path.is_symlink():
            raise ValueError("family take ledger must not be a symlink")
        self._requested_budget_units = _usd_to_micro_usd(total_budget_usd)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS budget (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    currency TEXT NOT NULL CHECK (currency = 'USD'),
                    total_micro_usd INTEGER NOT NULL CHECK (total_micro_usd >= 0)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    request_state_hash TEXT NOT NULL,
                    request_material_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('reserved','started','unknown','completed','failed','confirmed_zero')),
                    upper_bound_micro_usd INTEGER NOT NULL CHECK (upper_bound_micro_usd >= 0),
                    actual_usage_micro_usd INTEGER NULL CHECK (actual_usage_micro_usd IS NULL OR actual_usage_micro_usd >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind, request_state_hash)
                );
                CREATE TABLE IF NOT EXISTS provider_execution_receipts (
                    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id)
                        ON UPDATE RESTRICT ON DELETE RESTRICT,
                    request_sha256 TEXT NOT NULL CHECK (
                        length(request_sha256) = 64
                        AND request_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    response_sha256 TEXT NOT NULL CHECK (
                        length(response_sha256) = 64
                        AND response_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    interaction_id TEXT NOT NULL CHECK (
                        length(interaction_id) BETWEEN 1 AND 256
                    ),
                    provider_model TEXT NOT NULL CHECK (
                        length(provider_model) BETWEEN 1 AND 128
                    ),
                    receipt_sha256 TEXT NOT NULL UNIQUE CHECK (
                        length(receipt_sha256) = 64
                        AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'
                    ),
                    created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 1 AND 40)
                );
                CREATE TRIGGER IF NOT EXISTS provider_execution_receipts_no_update
                BEFORE UPDATE ON provider_execution_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'provider execution receipts are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS provider_execution_receipts_no_delete
                BEFORE DELETE ON provider_execution_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'provider execution receipts are immutable');
                END;
                CREATE TABLE IF NOT EXISTS budget_authorizations (
                    authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    old_total_micro_usd INTEGER NOT NULL CHECK (old_total_micro_usd >= 0),
                    new_total_micro_usd INTEGER NOT NULL CHECK (new_total_micro_usd >= 0),
                    held_micro_usd INTEGER NOT NULL CHECK (held_micro_usd >= 0),
                    authorized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS takes (
                    take_id TEXT PRIMARY KEY,
                    shot_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    request_hash TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
                    status TEXT NOT NULL CHECK (status IN ('reserved','submitting','unknown','submitted','polling','result_ready','publishing','done','failed')),
                    provider_job_id TEXT NULL,
                    provider_status TEXT NULL,
                    status_url TEXT NULL,
                    result_url TEXT NULL,
                    output_url TEXT NULL,
                    output_relpath TEXT NOT NULL UNIQUE,
                    output_sha256 TEXT NULL,
                    result_actual_micro_usd INTEGER NULL CHECK (
                        result_actual_micro_usd IS NULL OR result_actual_micro_usd >= 0
                    ),
                    error TEXT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS takes_shot_route ON takes(shot_id, route);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(takes)")}
            if "result_actual_micro_usd" not in columns:
                connection.execute(
                    "ALTER TABLE takes ADD COLUMN result_actual_micro_usd INTEGER NULL"
                )
            row = connection.execute("SELECT total_micro_usd FROM budget WHERE singleton = 1").fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO budget(singleton, currency, total_micro_usd) VALUES (1, 'USD', ?)",
                    (self._requested_budget_units,),
                )
            elif self._requested_budget_units and row["total_micro_usd"] != self._requested_budget_units:
                raise ValueError("project budget is immutable and differs from the requested total")

    @property
    def total_budget_micro_usd(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT total_micro_usd FROM budget WHERE singleton = 1").fetchone()[0])

    def authorize_budget_update(
        self,
        new_total_usd: Decimal | str | int | float,
        *,
        authorized: bool,
    ) -> BudgetAuthorization:
        """Explicitly replace the project ceiling without changing operation rows.

        This is the only supported way to move a ledger created for a zero-cost
        local preview to a later paid ceiling.  Reopening a ledger or passing a
        constructor value never resets reservations or settled usage.
        """
        if authorized is not True:
            raise PaidAuthorizationError("authorized=True is required to update the project budget")
        new_units = _usd_to_micro_usd(new_total_usd)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            old_units = int(connection.execute(
                "SELECT total_micro_usd FROM budget WHERE singleton = 1"
            ).fetchone()[0])
            held = self._held_units(connection)
            if new_units < held:
                connection.rollback()
                raise BudgetExceededError(
                    f"new budget {new_units} micro-USD is below held/settled cost {held} micro-USD"
                )
            changed = new_units != old_units
            if changed:
                connection.execute(
                    "UPDATE budget SET total_micro_usd = ? WHERE singleton = 1", (new_units,)
                )
                connection.execute(
                    """
                    INSERT INTO budget_authorizations(
                        old_total_micro_usd, new_total_micro_usd, held_micro_usd, authorized_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (old_units, new_units, held, _utc_now()),
                )
            connection.commit()
            return BudgetAuthorization(
                old_total_micro_usd=old_units,
                new_total_micro_usd=new_units,
                held_micro_usd=held,
                changed=changed,
            )

    @staticmethod
    def _held_units(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(
                CASE
                    WHEN status = 'confirmed_zero' THEN 0
                    WHEN status = 'completed' AND actual_usage_micro_usd IS NOT NULL THEN actual_usage_micro_usd
                    ELSE upper_bound_micro_usd
                END
            ), 0) FROM operations
            """
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _reservation(row: sqlite3.Row) -> OperationReservation:
        return OperationReservation(
            operation_id=row["operation_id"],
            kind=row["kind"],
            request_state_hash=row["request_state_hash"],
            status=row["status"],
            upper_bound_micro_usd=row["upper_bound_micro_usd"],
            actual_usage_micro_usd=row["actual_usage_micro_usd"],
        )

    def get_operation(self, operation_id: str) -> OperationReservation:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown operation_id: {operation_id}")
        return self._reservation(row)

    @staticmethod
    def _validate_receipt_operation(row: sqlite3.Row, request_sha256: str) -> None:
        if row["kind"] not in _REVIEW_OPERATION_KINDS:
            raise OperationStateError("provider receipts are allowed only for review operations")
        if row["status"] not in _RECEIPT_OPERATION_STATES:
            raise OperationStateError(
                "provider receipt requires a started, unknown, or completed review operation"
            )
        try:
            material = json.loads(row["request_material_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise OperationStateError("review operation request material is corrupt") from exc
        if not isinstance(material, dict):
            raise OperationStateError("review operation request material is not an object")
        bound_request_sha256 = material.get("request_sha256")
        if (
            not isinstance(bound_request_sha256, str)
            or not _SHA256_RE.fullmatch(bound_request_sha256)
        ):
            raise OperationStateError("review operation does not bind an exact request SHA-256")
        if bound_request_sha256 != request_sha256:
            raise OperationStateError("provider receipt request SHA-256 differs from the review operation")

    @staticmethod
    def _verified_receipt(row: sqlite3.Row) -> ProviderExecutionReceipt:
        try:
            receipt = ProviderExecutionReceipt(
                operation_id=row["operation_id"],
                request_sha256=row["request_sha256"],
                response_sha256=row["response_sha256"],
                interaction_id=row["interaction_id"],
                provider_model=row["provider_model"],
                receipt_sha256=row["receipt_sha256"],
                created_at=row["created_at"],
            )
            expected = provider_execution_receipt_sha256(
                operation_id=receipt.operation_id,
                request_sha256=receipt.request_sha256,
                response_sha256=receipt.response_sha256,
                interaction_id=receipt.interaction_id,
                provider_model=receipt.provider_model,
                created_at=receipt.created_at,
            )
        except (TypeError, ValueError, OperationStateError) as exc:
            raise OperationStateError("stored provider execution receipt is invalid") from exc
        if receipt.receipt_sha256 != expected:
            raise OperationStateError("stored provider execution receipt hash mismatch")
        return receipt

    def record_or_verify_provider_execution_receipt(
        self,
        *,
        operation_id: str,
        request_sha256: str,
        response_sha256: str,
        interaction_id: str,
        provider_model: str,
    ) -> ProviderExecutionReceipt:
        """Atomically insert one receipt or verify the existing receipt is identical."""
        if not isinstance(operation_id, str) or not _SAFE_ID_RE.fullmatch(operation_id):
            raise ValueError("operation_id must be a short stable identifier")
        request_digest = _validated_sha256(request_sha256, "request_sha256")
        response_digest = _validated_sha256(response_sha256, "response_sha256")
        interaction = _validated_provider_value(interaction_id, "interaction_id", maximum=256)
        model = _validated_provider_value(provider_model, "provider_model", maximum=128)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if operation is None:
                connection.rollback()
                raise KeyError(f"unknown operation_id: {operation_id}")
            self._validate_receipt_operation(operation, request_digest)
            existing = connection.execute(
                "SELECT * FROM provider_execution_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                receipt = self._verified_receipt(existing)
                if (
                    receipt.request_sha256 != request_digest
                    or receipt.response_sha256 != response_digest
                    or receipt.interaction_id != interaction
                    or receipt.provider_model != model
                ):
                    connection.rollback()
                    raise OperationStateError(
                        "provider execution receipt already exists with different immutable data"
                    )
                connection.commit()
                return receipt

            created_at = _utc_now()
            receipt_sha256 = provider_execution_receipt_sha256(
                operation_id=operation_id,
                request_sha256=request_digest,
                response_sha256=response_digest,
                interaction_id=interaction,
                provider_model=model,
                created_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO provider_execution_receipts(
                    operation_id, request_sha256, response_sha256, interaction_id,
                    provider_model, receipt_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id, request_digest, response_digest, interaction,
                    model, receipt_sha256, created_at,
                ),
            )
            stored = connection.execute(
                "SELECT * FROM provider_execution_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            receipt = self._verified_receipt(stored)
            connection.commit()
            return receipt

    def read_and_verify_provider_execution_receipt(
        self, operation_id: str,
    ) -> ProviderExecutionReceipt:
        """Return immutable receipt data after rechecking its operation and canonical hash."""
        if not isinstance(operation_id, str) or not _SAFE_ID_RE.fullmatch(operation_id):
            raise ValueError("operation_id must be a short stable identifier")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
            if operation is None:
                connection.rollback()
                raise KeyError(f"unknown operation_id: {operation_id}")
            receipt_row = connection.execute(
                "SELECT * FROM provider_execution_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if receipt_row is None:
                connection.rollback()
                raise KeyError(f"provider execution receipt is missing for operation: {operation_id}")
            receipt = self._verified_receipt(receipt_row)
            self._validate_receipt_operation(operation, receipt.request_sha256)
            connection.commit()
            return receipt

    def claim_operation(self, operation_id: str) -> bool:
        """Atomically claim a reserved generic operation for one worker.

        Exactly one concurrent caller receives ``True``.  An already-started,
        terminal, or unknown operation returns ``False`` and retains its hold.
        """
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE operations SET status = 'started', updated_at = ?
                WHERE operation_id = ? AND status = 'reserved'
                """,
                (_utc_now(), operation_id),
            )
            claimed = cursor.rowcount == 1
            if not claimed and connection.execute(
                "SELECT 1 FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone() is None:
                connection.rollback()
                raise KeyError(f"unknown operation_id: {operation_id}")
            connection.commit()
            return claimed

    def reserve_operation(
        self,
        *,
        kind: str,
        request_state_hash: str,
        upper_bound_usd: Decimal | str | int | float,
        request_material: Mapping[str, Any],
        operation_id: str | None = None,
    ) -> OperationReservation:
        """Atomically reserve a bounded amount, idempotently by kind/hash."""
        if not _SAFE_ID_RE.fullmatch(kind):
            raise ValueError("kind must be a short stable identifier")
        _validate_state_hash(request_state_hash)
        identifier = operation_id or _operation_id(kind, request_state_hash)
        if not _SAFE_ID_RE.fullmatch(identifier):
            raise ValueError("operation_id must be a short stable identifier")
        units = _usd_to_micro_usd(upper_bound_usd)
        material_json = _json_text(request_material)
        now = _utc_now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM operations WHERE kind = ? AND request_state_hash = ?",
                (kind, request_state_hash),
            ).fetchone()
            if existing is not None:
                if existing["upper_bound_micro_usd"] != units or existing["request_material_json"] != material_json:
                    raise OperationStateError("operation hash was reused with different request material or bound")
                connection.commit()
                return self._reservation(existing)
            budget = int(connection.execute("SELECT total_micro_usd FROM budget WHERE singleton = 1").fetchone()[0])
            held = self._held_units(connection)
            if held + units > budget:
                connection.rollback()
                raise BudgetExceededError(
                    f"reservation requires {units} micro-USD with {budget - held} micro-USD remaining"
                )
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, kind, request_state_hash, request_material_json, status,
                    upper_bound_micro_usd, actual_usage_micro_usd, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?, NULL, ?, ?)
                """,
                (identifier, kind, request_state_hash, material_json, units, now, now),
            )
            row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (identifier,)).fetchone()
            connection.commit()
            return self._reservation(row)

    def _transition(self, operation_id: str, status: str, *, actual_units: int | None = None) -> OperationReservation:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown operation_id: {operation_id}")
            allowed = {
                "started": {"reserved", "started"},
                "unknown": {"reserved", "started", "unknown"},
                "failed": {"reserved", "started", "unknown", "failed"},
                "completed": {"reserved", "started", "unknown", "completed"},
                "confirmed_zero": {"reserved", "started", "unknown", "failed", "confirmed_zero"},
            }
            if row["status"] not in allowed[status]:
                raise OperationStateError(f"cannot transition operation from {row['status']} to {status}")
            if actual_units is not None and actual_units > row["upper_bound_micro_usd"]:
                raise OperationStateError("actual usage exceeds the operation upper bound")
            actual_to_store = row["actual_usage_micro_usd"] if actual_units is None else actual_units
            connection.execute(
                "UPDATE operations SET status = ?, actual_usage_micro_usd = ?, updated_at = ? WHERE operation_id = ?",
                (status, actual_to_store, _utc_now(), operation_id),
            )
            updated = connection.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            connection.commit()
            return self._reservation(updated)

    def mark_started(self, operation_id: str) -> OperationReservation:
        if not self.claim_operation(operation_id):
            raise OperationStateError("operation was already claimed or is not reservable")
        return self.get_operation(operation_id)

    def mark_unknown(self, operation_id: str) -> OperationReservation:
        """Record ambiguity while retaining the full reservation."""
        return self._transition(operation_id, "unknown")

    def mark_failed(self, operation_id: str) -> OperationReservation:
        """Record failure while retaining the full reservation."""
        return self._transition(operation_id, "failed")

    def complete_operation(
        self, operation_id: str, *, actual_usage_usd: Decimal | str | int | float | None = None
    ) -> OperationReservation:
        """Complete an operation; omitted actual usage remains SQL NULL."""
        actual = None if actual_usage_usd is None else _usd_to_micro_usd(actual_usage_usd)
        return self._transition(operation_id, "completed", actual_units=actual)

    def confirm_zero_charge(self, operation_id: str) -> OperationReservation:
        """Release a hold only after external confirmation of zero charge."""
        return self._transition(operation_id, "confirmed_zero", actual_units=0)


def validate_credentialed_queue_url(url: str) -> str:
    """Accept only HTTPS queue.fal.run URLs before credentials are attached."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeProviderUrlError(f"refusing malformed provider URL: {url}") from exc
    # fal submission URLs contain the exact route, while returned status and
    # result URLs may collapse to the model-family queue namespace.
    allowed_route_prefixes = (
        "/fal-ai/kling-video/", "/fal-ai/veo3.1/", "/google/gemini-omni-flash/",
        "/minimax/h3-max/",
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "queue.fal.run"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith(allowed_route_prefixes)
        or parsed.query
        or parsed.fragment
    ):
        raise UnsafeProviderUrlError(f"refusing credentialed provider URL: {url}")
    return url


class FalQueueTransport:
    """No-retry fal queue transport with redirects disabled."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(follow_redirects=False)

    @staticmethod
    def _headers(credential: str) -> dict[str, str]:
        return {"Authorization": f"Key {credential}", "Content-Type": "application/json"}

    @staticmethod
    def _reject_redirect(response: httpx.Response) -> None:
        if 300 <= response.status_code < 400:
            raise UnsafeProviderUrlError("provider redirect rejected before forwarding credentials")

    def submit(self, route: str, payload: Mapping[str, Any], credential: str) -> QueueSubmission:
        url = validate_credentialed_queue_url(f"https://queue.fal.run/{route}")
        response = self._client.post(url, headers=self._headers(credential), json=dict(payload), timeout=120)
        self._reject_redirect(response)
        response.raise_for_status()
        data = response.json()
        return QueueSubmission(
            job_id=str(data["request_id"]),
            status_url=str(data["status_url"]),
            result_url=str(data["response_url"]),
            status=str(data.get("status", "QUEUED")),
        )

    def poll(self, status_url: str, credential: str) -> QueueStatus:
        url = validate_credentialed_queue_url(status_url)
        response = self._client.get(url, headers=self._headers(credential), timeout=30)
        self._reject_redirect(response)
        response.raise_for_status()
        data = response.json()
        return QueueStatus(status=str(data.get("status", "")), detail=data.get("error"))

    def fetch_result(self, result_url: str, credential: str) -> QueueResult:
        url = validate_credentialed_queue_url(result_url)
        response = self._client.get(url, headers=self._headers(credential), timeout=30)
        self._reject_redirect(response)
        response.raise_for_status()
        data = response.json()
        return QueueResult(output_url=str(data["video"]["url"]))

    def download(self, output_url: str) -> bytes:
        parsed = urlsplit(output_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise UnsafeProviderUrlError(f"refusing output URL: {output_url}")
        response = self._client.get(output_url, timeout=300)
        self._reject_redirect(response)
        response.raise_for_status()
        return response.content


class FamilyTakeExecutor:
    """Prepare, submit, resume, download, and verify immutable family takes."""

    def __init__(
        self,
        project_root: Path,
        transport: QueueTransport,
        *,
        total_budget_usd: Decimal | str | int | float = 0,
        local_renderer: LocalRenderer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.ledger = OperationLedger(project_root, total_budget_usd=total_budget_usd)
        self.project_root = self.ledger.project_root
        self.transport = transport
        self.local_renderer = local_renderer
        self.sleep = sleep

    def dry_run_plan(
        self, plan: FamilyShotPlan, prepared_by_source: Mapping[str, Any] | Any, *, today: date | None = None
    ) -> FamilyDryRunPlan:
        return dry_run_family_plan(plan, prepared_by_source, today=today)

    def _take_from_row(self, row: sqlite3.Row) -> TakeRecord:
        return TakeRecord(
            take_id=row["take_id"],
            shot_id=row["shot_id"],
            route=row["route"],
            request_hash=row["request_hash"],
            status=row["status"],
            provider_job_id=row["provider_job_id"],
            provider_status=row["provider_status"],
            status_url=row["status_url"],
            result_url=row["result_url"],
            output_url=row["output_url"],
            output_path=self.project_root / Path(row["output_relpath"]),
            output_sha256=row["output_sha256"],
            result_actual_micro_usd=row["result_actual_micro_usd"],
            upper_bound_micro_usd=row["upper_bound_micro_usd"],
            actual_usage_micro_usd=row["actual_usage_micro_usd"],
            error=row["error"],
        )

    def _load_take(self, take_id: str) -> TakeRecord:
        with closing(self.ledger._connect()) as connection:
            row = connection.execute(
                """
                SELECT t.*, o.upper_bound_micro_usd, o.actual_usage_micro_usd
                FROM takes t JOIN operations o ON o.operation_id = t.operation_id
                WHERE t.take_id = ?
                """,
                (take_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown take_id: {take_id}")
        return self._take_from_row(row)

    def _prepare_take(
        self, planned: PlannedTake, *, campaign_id: str | None = None,
    ) -> TakeRecord:
        request = planned.request
        if campaign_id is not None:
            _validate_state_hash(campaign_id)
        identifier = _take_id(request.request_hash)
        with closing(self.ledger._connect()) as connection:
            existing_take = connection.execute(
                "SELECT take_id FROM takes WHERE request_hash = ?", (request.request_hash,)
            ).fetchone()
            existing_count = int(connection.execute(
                "SELECT COUNT(*) FROM takes WHERE shot_id = ? AND route = ?",
                (request.shot_id, request.route),
            ).fetchone()[0])
        if existing_take is None and existing_count >= MAX_TAKES_PER_SHOT_ROUTE:
            raise TakeLimitError(
                f"shot {request.shot_id} already has {MAX_TAKES_PER_SHOT_ROUTE} takes for {request.route}"
            )
        request_material = request.stored_request_for_storage()
        if campaign_id is not None:
            request_material = {**request_material, "campaign_id": campaign_id}
        operation = self.ledger.reserve_operation(
            kind="family_video_take",
            request_state_hash=request.request_hash,
            upper_bound_usd=Decimal(planned.upper_bound_micro_usd) / MICRO_USD_PER_USD,
            request_material=request_material,
            operation_id=identifier,
        )
        output_relpath = Path("clips") / "raw" / (
            f"{request.shot_id}--{_route_slug(request.route)}--{identifier}.mp4"
        )
        now = _utc_now()
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM takes WHERE request_hash = ?", (request.request_hash,)).fetchone()
            if existing is None:
                count = int(connection.execute(
                    "SELECT COUNT(*) FROM takes WHERE shot_id = ? AND route = ?",
                    (request.shot_id, request.route),
                ).fetchone()[0])
                if count >= MAX_TAKES_PER_SHOT_ROUTE:
                    connection.rollback()
                    # The operation was just reserved in a preceding transaction.
                    # It is safe to release because no submission was started.
                    self.ledger.confirm_zero_charge(operation.operation_id)
                    raise TakeLimitError(
                        f"shot {request.shot_id} already has {MAX_TAKES_PER_SHOT_ROUTE} takes for {request.route}"
                    )
                connection.execute(
                    """
                    INSERT INTO takes(
                        take_id, shot_id, route, request_hash, request_json, operation_id,
                        status, output_relpath, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                    """,
                    (
                        identifier, request.shot_id, request.route, request.request_hash,
                        _json_text(request.stored_request_for_storage()), operation.operation_id,
                        output_relpath.as_posix(), now, now,
                    ),
                )
            connection.commit()
        return self._load_take(identifier)

    def _update_take(self, take_id: str, status: str, **fields: Any) -> TakeRecord:
        allowed = {
            "provider_job_id", "provider_status", "status_url", "result_url",
            "output_url", "output_sha256", "result_actual_micro_usd", "error",
        }
        if not set(fields) <= allowed:
            raise ValueError("unsupported take update field")
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, _utc_now()]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(take_id)
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"UPDATE takes SET {', '.join(assignments)} WHERE take_id = ?", values)
            connection.commit()
        return self._load_take(take_id)

    def _claim_take(self, take_id: str) -> bool:
        """Atomically claim both the generic operation and take for submission."""
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation_id, status FROM takes WHERE take_id = ?", (take_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"unknown take_id: {take_id}")
            if row["status"] != "reserved":
                connection.commit()
                return False
            claimed = connection.execute(
                """
                UPDATE operations SET status = 'started', updated_at = ?
                WHERE operation_id = ? AND status = 'reserved'
                """,
                (_utc_now(), row["operation_id"]),
            ).rowcount == 1
            if not claimed:
                connection.commit()
                return False
            changed = connection.execute(
                """
                UPDATE takes SET status = 'submitting', error = NULL, updated_at = ?
                WHERE take_id = ? AND status = 'reserved'
                """,
                (_utc_now(), take_id),
            ).rowcount == 1
            if not changed:
                connection.rollback()
                return False
            connection.commit()
            return True

    @staticmethod
    def _verify_done(record: TakeRecord) -> TakeRecord:
        path = record.output_path
        if path.is_symlink() or not path.is_file() or record.output_sha256 is None:
            raise OutputIntegrityError(f"completed take output is missing or unsafe: {record.take_id}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record.output_sha256:
            raise OutputIntegrityError(f"completed take output hash mismatch: {record.take_id}")
        return record

    def _publish_output(self, destination: Path, data: bytes, digest: str) -> None:
        expected_parent = self.project_root / "clips" / "raw"
        for directory in (self.project_root / "clips", expected_parent):
            if directory.is_symlink():
                raise OutputIntegrityError("take output path must not contain symlink directories")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.resolve() != expected_parent.resolve():
            raise OutputIntegrityError("take output path escaped the project clips/raw directory")
        if destination.is_symlink():
            raise OutputIntegrityError("take output must not be a symlink")
        if destination.exists():
            if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise OutputIntegrityError(f"immutable take output already exists with different bytes: {destination.name}")
            return
        staged_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".family-take-", suffix=".mp4", dir=destination.parent, delete=False) as staged:
                staged.write(data)
                staged.flush()
                os.fsync(staged.fileno())
                staged_name = staged.name
            try:
                os.link(staged_name, destination)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file():
                    raise OutputIntegrityError("concurrent take publication produced an unsafe path")
                if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                    raise OutputIntegrityError("concurrent take publication produced different bytes")
        finally:
            if staged_name is not None:
                Path(staged_name).unlink(missing_ok=True)

    def import_external_take(
        self, *, job_id: str, request: Mapping[str, Any], website_job_id: str,
        observation: Mapping[str, Any], video_path: Path, evidence_ref: str,
        ffprobe: str = "ffprobe",
    ) -> TakeRecord:
        """Publish a downloaded website MP4 as an ordinary, unreviewed family take."""
        if (
            not isinstance(job_id, str) or not _SAFE_ID_RE.fullmatch(job_id)
            or not isinstance(request.get("shot_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", request["shot_id"])
            or request.get("website") not in {
                "kling", "dreamina", "pixverse", "vidu", "flow",
                "krea", "seaart", "openart", "hailuo",
            }
            or not isinstance(website_job_id, str) or not _SAFE_ID_RE.fullmatch(website_job_id)
        ):
            raise ValueError("external take identifier is invalid")
        candidate = Path(video_path)
        if ".." in candidate.parts:
            raise OutputIntegrityError("external MP4 path contains traversal")
        for component in (candidate, *candidate.parents):
            if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                raise OutputIntegrityError("external MP4 path contains a link")
        if candidate.suffix.lower() != ".mp4" or not candidate.is_file():
            raise OutputIntegrityError("external MP4 must be an ordinary local file")
        if candidate.stat().st_size <= 0 or candidate.stat().st_size > 1_000_000_000:
            raise OutputIntegrityError("external MP4 size is invalid")
        try:
            probe = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=format_name,duration:stream=codec_type",
                 "-of", "json", str(candidate)],
                check=True, capture_output=True, text=True, timeout=30,
            )
            media = json.loads(probe.stdout)
            fmt = media["format"]
            if "mp4" not in fmt["format_name"].split(","):
                raise ValueError("container")
            if sum(item.get("codec_type") == "video" for item in media["streams"]) != 1:
                raise ValueError("video streams")
            duration = float(fmt["duration"])
            if not (0 < duration <= 3600):
                raise ValueError("duration")
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OutputIntegrityError("external MP4 did not pass media validation") from exc
        data = candidate.read_bytes()
        output_digest = hashlib.sha256(data).hexdigest()
        material = {
            "schema_version": "family-web-external-take/v1", "web_job_id": job_id,
            "plan_sha256": request["plan_sha256"], "shot_id": request["shot_id"],
            "route": f"web/{request['website']}", "website": request["website"],
            "website_job_id": website_job_id, "displayed_model": observation["displayed_model"],
            "displayed_controls": observation["selected_controls"],
            "displayed_cost": observation["displayed_cost"],
            "submission_evidence": observation.get("history_evidence", []),
            "download_evidence_ref": evidence_ref, "output_sha256": output_digest,
            "payload": {"sources": [
                {"source_id": source_id, "role": request["source_roles"][source_id],
                 "sha256": request["source_sha256"][source_id]}
                for source_id in request["source_ids"]
            ], "prompt": request["prompt"], "controls": request["controls"]},
        }
        request_hash = hashlib.sha256(b"family-external-take/v1\0" + _json_text(material).encode()).hexdigest()
        take_id = _take_id(request_hash)
        operation = self.ledger.reserve_operation(
            kind="family_web_external_take", request_state_hash=request_hash,
            upper_bound_usd=0, request_material=material, operation_id=take_id,
        )
        output_relpath = Path(CLIPS_DIRNAME) / CLIPS_RAW_DIRNAME / (
            f"{request['shot_id']}--web-{request['website']}--{take_id}.mp4"
        )
        with closing(self.ledger._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM takes WHERE take_id = ?", (take_id,)).fetchone()
            if existing is None:
                now = _utc_now()
                connection.execute(
                    """INSERT INTO takes(
                        take_id, shot_id, route, request_hash, request_json, operation_id,
                        status, provider_job_id, provider_status, output_relpath,
                        output_sha256, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'publishing', ?, 'IMPORTED', ?, ?, ?, ?)""",
                    (take_id, request["shot_id"], material["route"], request_hash,
                     _json_text(material), operation.operation_id, website_job_id,
                     output_relpath.as_posix(), output_digest, now, now),
                )
            elif existing["request_json"] != _json_text(material) or existing["output_sha256"] != output_digest:
                raise OutputIntegrityError("external take identity already has conflicting evidence")
            connection.commit()
        record = self._load_take(take_id)
        if record.status == "done":
            return self._verify_done(record)
        self._publish_output(record.output_path, data, output_digest)
        self.ledger.complete_operation(take_id, actual_usage_usd=0)
        return self._update_take(take_id, "done")

    def _run_prepared(
        self,
        planned: PlannedTake,
        record: TakeRecord,
        *,
        paid_authorized: bool,
        credential: str | None,
        max_polls: int,
        poll_interval_seconds: float,
    ) -> TakeRecord:
        request = planned.request
        if record.status == "done":
            return self._verify_done(record)
        if record.status == "submitting":
            # Another executor may still own the POST. A crash also leaves this
            # conservative state. In either case, never claim or resubmit it.
            return record
        if record.status in {"unknown", "failed"}:
            return record
        if record.status == "publishing":
            if record.output_path.is_file() and not record.output_path.is_symlink():
                self._verify_done(record)
            elif record.output_url is not None and record.output_sha256 is not None:
                try:
                    recovered = self.transport.download(record.output_url)
                except Exception as exc:
                    return self._update_take(
                        record.take_id, "publishing", error=_safe_error_code("publishing_download", exc)
                    )
                if hashlib.sha256(recovered).hexdigest() != record.output_sha256:
                    raise OutputIntegrityError("recovered provider output differs from the durable output hash")
                self._publish_output(record.output_path, recovered, record.output_sha256)
            elif request.capability.execution == "local" and self.local_renderer is not None and record.output_sha256:
                recovered = self.local_renderer(request)
                if hashlib.sha256(recovered).hexdigest() != record.output_sha256:
                    raise OutputIntegrityError("recovered local output differs from the durable output hash")
                self._publish_output(record.output_path, recovered, record.output_sha256)
            else:
                return record
            actual_usd = (
                None if record.result_actual_micro_usd is None
                else Decimal(record.result_actual_micro_usd) / MICRO_USD_PER_USD
            )
            self.ledger.complete_operation(record.take_id, actual_usage_usd=actual_usd)
            return self._update_take(record.take_id, "done", error=None)

        if request.capability.execution == "local":
            if self.local_renderer is None:
                raise FamilyTakeError("local/still-hold requires an injected local_renderer")
            if record.status == "reserved":
                if not self._claim_take(record.take_id):
                    return self._load_take(record.take_id)
                record = self._load_take(record.take_id)
                data = self.local_renderer(request)
                if not data:
                    self.ledger.mark_failed(record.take_id)
                    return self._update_take(record.take_id, "failed", error="local renderer returned an empty output")
                digest = hashlib.sha256(data).hexdigest()
                record = self._update_take(record.take_id, "publishing", output_sha256=digest, error=None)
                self._publish_output(record.output_path, data, digest)
                self.ledger.complete_operation(record.take_id, actual_usage_usd=0)
                return self._update_take(record.take_id, "done")
            return record

        if not paid_authorized:
            raise PaidAuthorizationError("paid_authorized=True is required for paid provider execution")
        if self.ledger.total_budget_micro_usd <= 0:
            raise PaidAuthorizationError("a nonzero persisted project budget is required for paid execution")
        if not credential:
            raise PaidAuthorizationError("provider credential is required after paid authorization")

        if record.status == "reserved":
            if not self._claim_take(record.take_id):
                return self._load_take(record.take_id)
            record = self._load_take(record.take_id)
            try:
                submission = self.transport.submit(
                    request.route, request.payload_for_submission(), credential
                )
                if not submission.job_id.strip():
                    raise FamilyTakeError("provider submission omitted job ID")
                status_url = validate_credentialed_queue_url(submission.status_url)
                result_url = validate_credentialed_queue_url(submission.result_url)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if 400 <= status_code < 500 and status_code not in {408, 409, 425, 429}:
                    # A definite client-side rejection has no provider job to
                    # recover. Release the reservation instead of leaving a
                    # phantom paid take in the ambiguous state.
                    self.ledger.confirm_zero_charge(record.take_id)
                    return self._update_take(
                        record.take_id,
                        "failed",
                        error=f"provider_submit_http_{status_code}",
                    )
                self.ledger.mark_unknown(record.take_id)
                return self._update_take(
                    record.take_id, "unknown", error=_safe_error_code("ambiguous_submission", exc)
                )
            except Exception as exc:
                self.ledger.mark_unknown(record.take_id)
                return self._update_take(
                    record.take_id, "unknown", error=_safe_error_code("ambiguous_submission", exc)
                )
            submitted_status = submission.status.upper()
            if submitted_status not in {"QUEUED", "IN_QUEUE", "IN_PROGRESS", "RUNNING"}:
                submitted_status = "QUEUED"
            record = self._update_take(
                record.take_id,
                "submitted",
                provider_job_id=submission.job_id,
                provider_status=submitted_status,
                status_url=status_url,
                result_url=result_url,
                error=None,
            )

        if record.status in {"submitted", "polling"}:
            if record.status_url is None or record.result_url is None or record.provider_job_id is None:
                self.ledger.mark_unknown(record.take_id)
                return self._update_take(record.take_id, "unknown", error="submitted take lacks durable provider identifiers")
            validate_credentialed_queue_url(record.status_url)
            validate_credentialed_queue_url(record.result_url)
            for poll_index in range(max_polls):
                if poll_interval_seconds > 0:
                    self.sleep(poll_interval_seconds)
                try:
                    polled = self.transport.poll(record.status_url, credential)
                except Exception as exc:
                    return self._update_take(
                        record.take_id, "submitted", error=_safe_error_code("poll_interrupted", exc)
                    )
                state = polled.status.upper()
                known_states = {
                    "QUEUED", "IN_QUEUE", "IN_PROGRESS", "RUNNING", "COMPLETED",
                    "ALREADY_COMPLETED", "FAILED", "CANCELLED",
                }
                stored_state = state if state in known_states else "UNRECOGNIZED"
                record = self._update_take(
                    record.take_id, "polling", provider_status=stored_state,
                    error="provider_status_detail" if polled.detail else None,
                )
                if state in {"COMPLETED", "ALREADY_COMPLETED"}:
                    record = self._update_take(record.take_id, "result_ready", provider_status=state, error=None)
                    break
                if state in {"FAILED", "CANCELLED"}:
                    self.ledger.mark_failed(record.take_id)
                    return self._update_take(
                        record.take_id, "failed", provider_status=state, error=f"provider_{state.lower()}"
                    )
            else:
                return record

        if record.status == "result_ready":
            if record.result_url is None:
                raise OperationStateError("result-ready take lacks a result URL")
            if record.output_url is None:
                try:
                    result = self.transport.fetch_result(record.result_url, credential)
                except httpx.HTTPStatusError as exc:
                    status_code = exc.response.status_code
                    if 400 <= status_code < 500 and status_code not in {408, 409, 425, 429}:
                        self.ledger.mark_failed(record.take_id)
                        return self._update_take(
                            record.take_id,
                            "failed",
                            error=f"provider_result_http_{status_code}",
                        )
                    return self._update_take(
                        record.take_id, "result_ready", error=_safe_error_code("result_fetch_interrupted", exc)
                    )
                except Exception as exc:
                    return self._update_take(
                        record.take_id, "result_ready", error=_safe_error_code("result_fetch_interrupted", exc)
                    )
                actual_units = None
                if result.actual_usage_usd is not None:
                    actual_units = _usd_to_micro_usd(result.actual_usage_usd)
                    if actual_units > record.upper_bound_micro_usd:
                        raise OperationStateError("provider actual usage exceeds reserved upper bound")
                record = self._update_take(
                    record.take_id, "result_ready", output_url=result.output_url,
                    result_actual_micro_usd=actual_units, error=None,
                )
            assert record.output_url is not None
            try:
                data = self.transport.download(record.output_url)
            except Exception as exc:
                return self._update_take(
                    record.take_id, "result_ready", error=_safe_error_code("download_interrupted", exc)
                )
            if not data:
                return self._update_take(record.take_id, "result_ready", error="provider output download was empty")
            digest = hashlib.sha256(data).hexdigest()
            record = self._update_take(record.take_id, "publishing", output_sha256=digest, error=None)
            self._publish_output(record.output_path, data, digest)
            # fal result payloads normally omit invoice data. Keep actual usage
            # NULL unless an integration explicitly supplies a trusted amount.
            actual_usd = (
                None if record.result_actual_micro_usd is None
                else Decimal(record.result_actual_micro_usd) / MICRO_USD_PER_USD
            )
            self.ledger.complete_operation(record.take_id, actual_usage_usd=actual_usd)
            return self._update_take(record.take_id, "done")
        return record

    def execute_take(
        self,
        shot: FamilyShot,
        prepared_by_source: Mapping[str, Any] | Any,
        *,
        incoming_join: Any = None,
        outgoing_join: Any = None,
        paid_authorized: bool = False,
        credential: str | None = None,
        today: date | None = None,
        max_polls: int = 30,
        poll_interval_seconds: float = 0,
        campaign_id: str | None = None,
    ) -> TakeRecord:
        """Preflight and execute one take; repeated calls resume by request hash."""
        if isinstance(max_polls, bool) or not isinstance(max_polls, int) or max_polls < 0:
            raise ValueError("max_polls must be a non-negative integer")
        # Build the single request directly because FamilyShotPlan requires the
        # source catalog. The same price path is used by plan dry-runs.
        request = build_provider_request(
            shot,
            _prepared_mapping(prepared_by_source),
            incoming_join=incoming_join,
            outgoing_join=outgoing_join,
        )
        if request.capability.execution == "local":
            quote = {"status": "local", "base_usd": 0}
            units = 0
        else:
            quote = estimate_video_cost([request.duration_seconds], request.route, today=today)
            if quote.get("status") != "estimate" or quote.get("base_usd") is None:
                raise PriceUnavailableError(str(quote.get("reason", "price unavailable")))
            units = _usd_to_micro_usd(quote["base_usd"])
        planned = PlannedTake(request=request, upper_bound_micro_usd=units, price_quote=quote)
        if request.capability.execution != "local":
            if not paid_authorized:
                raise PaidAuthorizationError("paid_authorized=True is required for paid provider execution")
            if self.ledger.total_budget_micro_usd <= 0:
                raise PaidAuthorizationError("a nonzero persisted project budget is required for paid execution")
            if not credential:
                raise PaidAuthorizationError("provider credential is required after paid authorization")
        record = self._prepare_take(planned, campaign_id=campaign_id)
        return self._run_prepared(
            planned, record, paid_authorized=paid_authorized, credential=credential,
            max_polls=max_polls, poll_interval_seconds=poll_interval_seconds,
        )

    def execute_plan(
        self,
        plan: FamilyShotPlan,
        prepared_by_source: Mapping[str, Any] | Any,
        *,
        paid_authorized: bool = False,
        credential: str | None = None,
        today: date | None = None,
        max_polls: int = 30,
        poll_interval_seconds: float = 0,
        campaign_id: str | None = None,
    ) -> tuple[TakeRecord, ...]:
        """Preflight every shot and reserve every take before the first POST."""
        dry_run = self.dry_run_plan(plan, prepared_by_source, today=today)
        if any(item.upper_bound_micro_usd for item in dry_run.shots):
            if not paid_authorized or self.ledger.total_budget_micro_usd <= 0 or not credential:
                raise PaidAuthorizationError(
                    "paid_authorized=True, a nonzero budget, and a credential are required for paid execution"
                )
        prepared_records = [
            self._prepare_take(item, campaign_id=campaign_id) for item in dry_run.shots
        ]
        return tuple(
            self._run_prepared(
                item, record, paid_authorized=paid_authorized, credential=credential,
                max_polls=max_polls, poll_interval_seconds=poll_interval_seconds,
            )
            for item, record in zip(dry_run.shots, prepared_records, strict=True)
        )


__all__ = [
    "BudgetExceededError",
    "BudgetAuthorization",
    "DEFAULT_LEDGER_NAME",
    "FalQueueTransport",
    "FamilyDryRunPlan",
    "FamilyTakeError",
    "FamilyTakeExecutor",
    "LocalRenderer",
    "MAX_TAKES_PER_SHOT_ROUTE",
    "MICRO_USD_PER_USD",
    "OperationLedger",
    "OperationReservation",
    "OperationStateError",
    "OutputIntegrityError",
    "PaidAuthorizationError",
    "PlannedTake",
    "PriceUnavailableError",
    "PROVIDER_EXECUTION_RECEIPT_SCHEMA",
    "ProviderExecutionReceipt",
    "QueueResult",
    "QueueStatus",
    "QueueSubmission",
    "QueueTransport",
    "TakeLimitError",
    "TakeRecord",
    "UnsafeProviderUrlError",
    "dry_run_family_plan",
    "provider_execution_receipt_sha256",
    "operation_id_for",
    "validate_credentialed_queue_url",
]
