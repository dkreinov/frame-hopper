"""Strict provider preflight and request construction for family-film shots.

The public entry point is :func:`build_provider_request`.  It validates a
``FamilyShot`` against an exact route capability, builds the provider prompt,
and returns both an in-memory submission payload and compact credential-free
request material suitable for the durable take ledger.

Local image bytes are encoded only in the in-memory payload.  The stored form
contains source IDs and SHA-256 digests, and the request hash covers the
canonical stored material plus the actual source-byte snapshots.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from PIL import Image, UnidentifiedImageError

from backend.services.family_shots import (
    FAL_GEMINI_OMNI_FLASH_V11_IMAGE_TO_VIDEO,
    FAL_KLING_O3_STANDARD_IMAGE_TO_VIDEO,
    FAL_KLING_O3_STANDARD_REFERENCE_TO_VIDEO,
    FAL_KLING_V3_PRO_IMAGE_TO_VIDEO,
    FAL_KLING_V3_STANDARD_IMAGE_TO_VIDEO,
    FAL_MINIMAX_H3_MAX_IMAGE_TO_VIDEO,
    FAL_VEO31_FAST_FIRST_LAST_FRAME_TO_VIDEO,
    LOCAL_STILL_HOLD_ROUTE,
    ContinuationFromExtractedFrameJoin,
    FamilyShot,
    FullFrameOcclusionJoin,
    MatchOnActionJoin,
    OverlappingMotionHandlesJoin,
)


class ProviderPreflightError(ValueError):
    """The shot cannot be represented by its selected provider route."""


MAX_PROVIDER_IMAGES = 16
MAX_SOURCE_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ProviderCapability:
    """Small, explicit capability record for one exact route."""

    route: str
    execution: Literal["fal_queue", "local"]
    start_image_field: str | None
    supports_end_image: bool
    supports_character_elements: bool
    duration_min: int | None
    duration_max: int | None
    end_image_field: str | None = "end_image_url"
    requires_end_image: bool = False
    allowed_durations: tuple[int, ...] | None = None
    duration_suffix: str = ""
    duration_is_integer: bool = False
    fixed_payload_fields: tuple[tuple[str, Any], ...] = ()
    max_source_image_bytes: int = MAX_SOURCE_IMAGE_BYTES
    send_generate_audio: bool = True
    max_character_elements: int | None = None
    max_supporting_reference_images: int | None = None
    max_prompt_characters: int | None = None


@dataclass(frozen=True)
class ProviderRequest:
    """Validated request with ephemeral and durable representations."""

    capability: ProviderCapability
    shot_id: str
    prompt: str
    duration_seconds: int | float
    payload: Mapping[str, Any]
    payload_hash: str
    stored_request: Mapping[str, Any]
    request_hash: str
    source_sha256: Mapping[str, str]

    @property
    def route(self) -> str:
        return self.capability.route

    def payload_for_submission(self) -> dict[str, Any]:
        """Return a mutable copy only if it still matches the frozen snapshot."""
        payload = _deep_thaw(self.payload)
        if hashlib.sha256(_canonical_json(payload)).hexdigest() != self.payload_hash:
            raise ProviderPreflightError("provider payload no longer matches its validated snapshot")
        return payload

    def stored_request_for_storage(self) -> dict[str, Any]:
        """Return the compact credential-free request material as plain JSON values."""
        return _deep_thaw(self.stored_request)


_CAPABILITIES = {
    FAL_KLING_O3_STANDARD_IMAGE_TO_VIDEO: ProviderCapability(
        route=FAL_KLING_O3_STANDARD_IMAGE_TO_VIDEO,
        execution="fal_queue",
        start_image_field="image_url",
        supports_end_image=True,
        supports_character_elements=False,
        duration_min=3,
        duration_max=15,
    ),
    FAL_KLING_O3_STANDARD_REFERENCE_TO_VIDEO: ProviderCapability(
        route=FAL_KLING_O3_STANDARD_REFERENCE_TO_VIDEO,
        execution="fal_queue",
        start_image_field="start_image_url",
        supports_end_image=True,
        supports_character_elements=True,
        duration_min=3,
        duration_max=15,
        fixed_payload_fields=(("shot_type", "customize"),),
        max_character_elements=3,
        max_supporting_reference_images=3,
        max_prompt_characters=2500,
    ),
    FAL_KLING_V3_PRO_IMAGE_TO_VIDEO: ProviderCapability(
        route=FAL_KLING_V3_PRO_IMAGE_TO_VIDEO,
        execution="fal_queue",
        start_image_field="start_image_url",
        supports_end_image=True,
        supports_character_elements=True,
        duration_min=3,
        duration_max=15,
        max_character_elements=3,
        max_supporting_reference_images=3,
    ),
    FAL_KLING_V3_STANDARD_IMAGE_TO_VIDEO: ProviderCapability(
        route=FAL_KLING_V3_STANDARD_IMAGE_TO_VIDEO,
        execution="fal_queue",
        start_image_field="start_image_url",
        supports_end_image=True,
        supports_character_elements=True,
        duration_min=3,
        duration_max=15,
        max_character_elements=3,
        max_supporting_reference_images=3,
    ),
    FAL_VEO31_FAST_FIRST_LAST_FRAME_TO_VIDEO: ProviderCapability(
        route=FAL_VEO31_FAST_FIRST_LAST_FRAME_TO_VIDEO,
        execution="fal_queue",
        start_image_field="first_frame_url",
        end_image_field="last_frame_url",
        supports_end_image=True,
        requires_end_image=True,
        supports_character_elements=False,
        duration_min=4,
        duration_max=8,
        allowed_durations=(4, 6, 8),
        duration_suffix="s",
        fixed_payload_fields=(("resolution", "720p"),),
        max_source_image_bytes=8 * 1024 * 1024,
    ),
    FAL_GEMINI_OMNI_FLASH_V11_IMAGE_TO_VIDEO: ProviderCapability(
        route=FAL_GEMINI_OMNI_FLASH_V11_IMAGE_TO_VIDEO,
        execution="fal_queue",
        start_image_field="image_url",
        supports_end_image=True,
        supports_character_elements=False,
        duration_min=3,
        duration_max=10,
        duration_is_integer=True,
        fixed_payload_fields=(("aspect_ratio", "16:9"), ("resolution", "720p")),
        send_generate_audio=False,
    ),
    FAL_MINIMAX_H3_MAX_IMAGE_TO_VIDEO: ProviderCapability(
        route=FAL_MINIMAX_H3_MAX_IMAGE_TO_VIDEO,
        execution="fal_queue",
        start_image_field="image_url",
        supports_end_image=True,
        supports_character_elements=False,
        duration_min=5,
        duration_max=15,
        duration_is_integer=True,
        fixed_payload_fields=(
            ("resolution", "768P"),
            ("prompt_expansion_mode", "balanced"),
            ("enable_safety_checker", True),
        ),
        send_generate_audio=False,
    ),
    LOCAL_STILL_HOLD_ROUTE: ProviderCapability(
        route=LOCAL_STILL_HOLD_ROUTE,
        execution="local",
        start_image_field=None,
        supports_end_image=False,
        supports_character_elements=False,
        duration_min=None,
        duration_max=None,
    ),
}

PROVIDER_REGISTRY: Mapping[str, ProviderCapability] = MappingProxyType(_CAPABILITIES)


def get_provider_capability(route: str) -> ProviderCapability:
    """Return the capability for an exact route ID; aliases are not accepted."""
    try:
        return PROVIDER_REGISTRY[route]
    except KeyError as exc:
        raise ProviderPreflightError(f"unsupported provider route: {route}") from exc


JoinInstruction = (
    ContinuationFromExtractedFrameJoin
    | FullFrameOcclusionJoin
    | MatchOnActionJoin
    | OverlappingMotionHandlesJoin
)


def _seconds_range(start: float, end: float) -> str:
    return f"[{start:g}s,{end:g}s)"


def _join_prompt_clause(
    shot: FamilyShot,
    join: JoinInstruction,
    *,
    side: Literal["incoming", "outgoing"],
) -> str:
    """Compile one plan-level handoff into the adjacent provider prompt."""
    expected_shot_id = (
        join.incoming_shot_id if side == "incoming" else join.outgoing_shot_id
    )
    if shot.shot_id != expected_shot_id:
        raise ProviderPreflightError(
            f"{side} join does not belong to shot {shot.shot_id}"
        )
    prefix = "Incoming" if side == "incoming" else "Outgoing"
    if isinstance(join, ContinuationFromExtractedFrameJoin):
        if side == "incoming":
            return (
                f"{prefix} join, extracted-frame continuation: provider frame zero is "
                f"{join.extracted_frame_id} from outgoing frame "
                f"{join.outgoing_extraction_frame_index}; continue its camera and subject motion "
                "immediately with no settling pause or pose reset"
            )
        return (
            f"{prefix} join, extracted-frame continuation: keep motion active through frame "
            f"{join.outgoing_extraction_frame_index}, which becomes the next provider's frame zero"
        )
    if isinstance(join, FullFrameOcclusionJoin):
        interval = (
            join.incoming_occlusion if side == "incoming" else join.outgoing_occlusion
        )
        return (
            f"{prefix} join, full-frame occlusion during "
            f"{_seconds_range(interval.start_seconds, interval.end_seconds)}: "
            f"{join.occluder_match_description} The moving occluder must cover the complete frame"
        )
    if isinstance(join, MatchOnActionJoin):
        interval = join.incoming_action if side == "incoming" else join.outgoing_action
        return (
            f"{prefix} join, match on action during "
            f"{_seconds_range(interval.start_seconds, interval.end_seconds)}: "
            f"{join.action_phase_description} Preserve the described action phase and direction"
        )
    interval = join.incoming_handle if side == "incoming" else join.outgoing_handle
    return (
        f"{prefix} join, overlapping moving handle during "
        f"{_seconds_range(interval.start_seconds, interval.end_seconds)}: "
        f"{join.visual_action_correspondence} Preserve the same visual position, action phase, and momentum"
    )


def build_provider_prompt(
    shot: FamilyShot,
    *,
    incoming_join: JoinInstruction | None = None,
    outgoing_join: JoinInstruction | None = None,
) -> str:
    """Render the declared action and continuity constraints concisely."""
    clauses = [
        f"Primary viewer focus and action: {shot.primary_action}",
        f"Start state: {shot.start_state}",
        f"End state: {shot.end_state}",
    ]
    if shot.visible_people:
        people = "; ".join(
            f"{person.person_id}: {person.visual_cues}" for person in shot.visible_people
        )
        clauses.append(f"Visible people: {people}")
    if shot.contact_constraints:
        contacts = "; ".join(
            f"{', '.join(item.participant_ids)}: {item.requirement}"
            for item in shot.contact_constraints
        )
        clauses.append(f"Contact/support constraints: {contacts}")
    if shot.intentional_age_changes:
        aging = "; ".join(
            f"{item.person_id}: {item.description}"
            for item in shot.intentional_age_changes
        )
        clauses.append(f"Allowed aging: {aging}")
    else:
        clauses.append("Aging constraint: no intentional age change")
    if shot.allowed_events:
        events = "; ".join(
            f"{item.person_id} {item.start_seconds:g}-{item.end_seconds:g}s: {item.description}"
            for item in shot.allowed_events
        )
        clauses.append(f"Allowed events: {events}")
    if shot.character_elements:
        references = "; ".join(
            f"{shot.character_element_token(item.element_id)} represents {item.person_id}"
            for item in shot.character_elements
        )
        clauses.append(f"Character references: {references}")
    if shot.continuous_motion is not None:
        motion = shot.continuous_motion
        clauses.extend((
            "Continuous physical/environment motion: "
            f"{motion.meaningful_physical_or_environment_motion}",
            f"Camera path: {motion.camera_path}",
            f"Entry motion: {motion.entry_direction}, {motion.entry_momentum} momentum",
            f"Exit motion: {motion.exit_direction}, {motion.exit_momentum} momentum",
            f"Motivated handoff device: {motion.motivated_handoff_device}",
            "Static fallback forbidden: keep the scene and camera moving throughout",
        ))
    if incoming_join is not None:
        clauses.append(_join_prompt_clause(shot, incoming_join, side="incoming"))
    if outgoing_join is not None:
        clauses.append(_join_prompt_clause(shot, outgoing_join, side="outgoing"))
    return ". ".join(clause.rstrip(". ") for clause in clauses) + "."


def _prepared_path(value: Any, source_id: str) -> Path:
    candidate = getattr(value, "prepared_path", value)
    try:
        path = Path(candidate)
    except TypeError as exc:
        raise ProviderPreflightError(f"invalid prepared source for {source_id}") from exc
    if path.is_symlink() or not path.is_file():
        raise ProviderPreflightError(f"prepared source is not a regular file: {source_id}")
    return path


def _validated_mime_type(data: bytes, source_id: str) -> str:
    """Decode enough of the image to validate bytes rather than its suffix."""
    try:
        with Image.open(BytesIO(data)) as image:
            image.verify()
            image_format = image.format
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ProviderPreflightError(f"prepared source is not a valid image: {source_id}") from exc
    mime_by_format = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    try:
        return mime_by_format[image_format]
    except KeyError as exc:
        raise ProviderPreflightError(f"unsupported prepared image format for {source_id}: {image_format}") from exc


def _data_uri(data: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(child) for child in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(child) for child in value]
    return value


def _paid_duration(shot: FamilyShot, capability: ProviderCapability) -> int:
    value = shot.timing.generation_duration_seconds
    if isinstance(value, bool) or not float(value).is_integer():
        raise ProviderPreflightError("paid provider duration must be an integer number of seconds")
    duration = int(value)
    if capability.duration_min is None or capability.duration_max is None:
        raise ProviderPreflightError("paid route has no duration capability")
    if not capability.duration_min <= duration <= capability.duration_max:
        raise ProviderPreflightError(
            f"duration must be {capability.duration_min}..{capability.duration_max} seconds for {capability.route}"
        )
    if capability.allowed_durations is not None and duration not in capability.allowed_durations:
        allowed = ", ".join(str(item) for item in capability.allowed_durations)
        raise ProviderPreflightError(f"duration must be one of {allowed} seconds for {capability.route}")
    return duration


def build_provider_request(
    shot: FamilyShot,
    prepared_by_source: Mapping[str, Any],
    *,
    image_url_encoder: Callable[[bytes, str], str] = _data_uri,
    incoming_join: JoinInstruction | None = None,
    outgoing_join: JoinInstruction | None = None,
) -> ProviderRequest:
    """Preflight one shot and construct its exact provider payload.

    ``prepared_by_source`` may map source IDs directly to paths or to
    ``PreparedSource`` instances.  The encoder is injectable for offline tests
    and future upload transports; its output is never written to the ledger.
    """
    capability = get_provider_capability(shot.provider_route)
    if capability.execution == "local" and shot.mode != "still_hold":
        raise ProviderPreflightError("local/still-hold accepts only still_hold shots")
    if capability.execution == "fal_queue" and shot.mode == "still_hold":
        raise ProviderPreflightError("still_hold must use local/still-hold")

    role_source: dict[str, str] = {}
    for item in shot.sources:
        if item.role in {"start", "end"}:
            role_source[item.role] = item.source_id
    reference_ids = {item.source_id for item in shot.sources if item.role == "reference"}

    if capability.requires_end_image and "end" not in role_source:
        raise ProviderPreflightError(f"{capability.route} requires both a start and end image")

    if reference_ids and not capability.supports_character_elements:
        raise ProviderPreflightError(f"{capability.route} does not support reference inputs")
    if shot.character_elements and not capability.supports_character_elements:
        raise ProviderPreflightError(f"{capability.route} does not support character elements")
    if (
        capability.max_character_elements is not None
        and len(shot.character_elements) > capability.max_character_elements
    ):
        raise ProviderPreflightError(
            f"{capability.route} supports at most {capability.max_character_elements} character elements"
        )
    if capability.max_supporting_reference_images is not None:
        for element in shot.character_elements:
            if len(element.supporting_reference_source_ids) > capability.max_supporting_reference_images:
                raise ProviderPreflightError(
                    f"{capability.route} supports at most "
                    f"{capability.max_supporting_reference_images} supporting reference images per element"
                )
    consumed_references = {
        source_id
        for element in shot.character_elements
        for source_id in (element.frontal_source_id, *element.supporting_reference_source_ids)
    }
    if reference_ids != consumed_references:
        unused = sorted(reference_ids - consumed_references)
        missing = sorted(consumed_references - reference_ids)
        details = []
        if unused:
            details.append(f"ungrouped reference inputs: {unused}")
        if missing:
            details.append(f"element inputs without reference role: {missing}")
        raise ProviderPreflightError("; ".join(details) or "reference inputs must be grouped into elements")

    used_ids = [item.source_id for item in shot.sources]
    if len(used_ids) > MAX_PROVIDER_IMAGES:
        raise ProviderPreflightError(f"provider request supports at most {MAX_PROVIDER_IMAGES} unique images")
    missing_prepared = sorted(set(used_ids) - set(prepared_by_source))
    if missing_prepared:
        raise ProviderPreflightError(f"missing prepared sources: {missing_prepared}")

    source_bytes: dict[str, bytes] = {}
    source_mimes: dict[str, str] = {}
    source_digests: dict[str, str] = {}
    total_source_bytes = 0
    for source_id in used_ids:
        path = _prepared_path(prepared_by_source[source_id], source_id)
        data = path.read_bytes()
        if not data:
            raise ProviderPreflightError(f"prepared source is empty: {source_id}")
        if len(data) > capability.max_source_image_bytes:
            raise ProviderPreflightError(
                f"prepared source exceeds {capability.max_source_image_bytes} bytes: {source_id}"
            )
        total_source_bytes += len(data)
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise ProviderPreflightError(
                f"provider request source bytes exceed {MAX_TOTAL_SOURCE_BYTES}"
            )
        source_bytes[source_id] = data
        source_mimes[source_id] = _validated_mime_type(data, source_id)
        source_digests[source_id] = hashlib.sha256(data).hexdigest()

    prompt = build_provider_prompt(
        shot, incoming_join=incoming_join, outgoing_join=outgoing_join,
    )
    if capability.max_prompt_characters is not None and len(prompt) > capability.max_prompt_characters:
        raise ProviderPreflightError(
            f"compiled prompt has {len(prompt)} characters; {capability.route} allows at most "
            f"{capability.max_prompt_characters}"
        )
    if capability.execution == "local":
        duration = shot.timing.generation_duration_seconds
        payload: dict[str, Any] = {
            "source_id": role_source["start"],
            "duration_seconds": shot.timing.generation_duration_seconds,
        }
        stored_payload: dict[str, Any] = {
            "source": {"source_id": role_source["start"], "sha256": source_digests[role_source["start"]]},
            "duration_seconds": shot.timing.generation_duration_seconds,
        }
    else:
        duration = _paid_duration(shot, capability)

        def encoded(source_id: str) -> str:
            return image_url_encoder(source_bytes[source_id], source_mimes[source_id])

        def descriptor(source_id: str) -> dict[str, str]:
            return {"source_id": source_id, "sha256": source_digests[source_id]}

        assert capability.start_image_field is not None
        provider_duration: int | str = (
            duration if capability.duration_is_integer else f"{duration}{capability.duration_suffix}"
        )
        payload = {
            capability.start_image_field: encoded(role_source["start"]),
            "prompt": prompt,
            "duration": provider_duration,
        }
        stored_payload = {
            capability.start_image_field: descriptor(role_source["start"]),
            "prompt": prompt,
            "duration": provider_duration,
        }
        if capability.send_generate_audio:
            payload["generate_audio"] = False
            stored_payload["generate_audio"] = False
        for field, value in capability.fixed_payload_fields:
            payload[field] = value
            stored_payload[field] = value
        if "end" in role_source:
            if not capability.supports_end_image:
                raise ProviderPreflightError(f"{capability.route} does not support an end image")
            if capability.end_image_field is None:
                raise ProviderPreflightError(f"{capability.route} has no end-image field")
            payload[capability.end_image_field] = encoded(role_source["end"])
            stored_payload[capability.end_image_field] = descriptor(role_source["end"])
        if shot.character_elements:
            payload["elements"] = [
                {
                    "frontal_image_url": encoded(element.frontal_source_id),
                    "reference_image_urls": [encoded(source_id) for source_id in element.supporting_reference_source_ids],
                }
                for element in shot.character_elements
            ]
            stored_payload["elements"] = [
                {
                    "token": shot.character_element_token(element.element_id),
                    "person_id": element.person_id,
                    "frontal_image": descriptor(element.frontal_source_id),
                    "reference_images": [descriptor(source_id) for source_id in element.supporting_reference_source_ids],
                }
                for element in shot.character_elements
            ]

    stored_request: dict[str, Any] = {
        "schema_version": "family-provider-request/v1",
        "shot_id": shot.shot_id,
        "route": capability.route,
        "payload": stored_payload,
    }
    hasher = hashlib.sha256(_canonical_json(stored_request))
    for source_id in sorted(source_bytes):
        identifier = source_id.encode("utf-8")
        hasher.update(len(identifier).to_bytes(4, "big"))
        hasher.update(identifier)
        hasher.update(len(source_bytes[source_id]).to_bytes(8, "big"))
        hasher.update(source_bytes[source_id])

    payload_hash = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return ProviderRequest(
        capability=capability,
        shot_id=shot.shot_id,
        prompt=prompt,
        duration_seconds=duration,
        payload=_deep_freeze(payload),
        payload_hash=payload_hash,
        stored_request=_deep_freeze(stored_request),
        request_hash=hasher.hexdigest(),
        source_sha256=MappingProxyType(source_digests),
    )


__all__ = [
    "PROVIDER_REGISTRY",
    "MAX_PROVIDER_IMAGES",
    "MAX_SOURCE_IMAGE_BYTES",
    "MAX_TOTAL_SOURCE_BYTES",
    "ProviderCapability",
    "ProviderPreflightError",
    "ProviderRequest",
    "JoinInstruction",
    "build_provider_prompt",
    "build_provider_request",
    "get_provider_capability",
]
