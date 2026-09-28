"""Versioned, provider-neutral contract for a family-film shot plan.

This module only validates creative intent and local source references.  It does
not submit work, inspect provider capabilities, infer relationships, or turn a
shot into a prompt.  Provider capability checks deliberately belong in the
separate provider-registry module planned for the next milestone.

The current route constants are exact fal route identifiers, while the contract
still accepts a syntactically valid explicit route for a future registry to
validate.  ``fal-ai/kling-video/v3/pro/image-to-video`` requires an initial
frame; its adapter maps ``character_elements`` in list order to ``@Element1``,
``@Element2``, and so on.  Every element records one frontal source ID and its
supporting reference source IDs, so sources for two people cannot be flattened
into an ambiguous reference list.  The adapter must send ``audio: false`` and
convert the validated numeric generation duration to the provider's documented
duration string; that route-specific capability validation is intentionally
outside this module.

Minimal JSON accepted by :class:`FamilyShotPlan`::

    {
      "schema_version": "family-shot-plan/v1",
      "sources": [{"source_id": "school_photo", "local_path": "school-photo.jpg"}],
      "shots": [{
        "shot_id": "school_hold",
        "mode": "still_hold",
        "provider_route": "local/still-hold",
        "sources": [{"source_id": "school_photo", "role": "start"}],
        "visible_people": [{"person_id": "person_a", "visual_cues": "person in the source photo"}],
        "contact_constraints": [],
        "primary_action": "hold the source image without generated motion",
        "start_state": "the source image is visible",
        "end_state": "the source image remains visible",
        "intentional_age_changes": [],
        "timing": {
          "generation_duration_seconds": 3.0,
          "generation_duration_reason": "a three-second hold establishes the memory",
          "screen_duration_seconds": 3.0,
          "screen_duration_reason": "the whole hold is used in the edit"
        }
      }]
    }

Use ``FamilyShotPlan.model_validate(payload)`` at the CLI/API boundary.  The
``source_id`` and ``shot_id`` values are opaque stable IDs: they cannot be an
integer sequence or a filename.  ``local_path`` is a relative POSIX path below
the source root; :func:`backend.services.family_prepare.prepare_local_sources`
performs the filesystem and symlink-containment check before opening it.
"""
from __future__ import annotations

import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


StableId = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
ProviderRoute = Annotated[str, Field(
    min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$",
)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
ShotMode = Literal["still_hold", "image_action", "continuous_bridge", "reference_scene"]
SourceRole = Literal["start", "end", "reference"]
MotionDirection = Literal[
    "left_to_right", "right_to_left", "forward", "backward", "up", "down",
]
MomentumState = Literal["slow", "steady", "accelerating", "decelerating"]
FilmFormKind = Literal[
    "continuous_moving_transitions", "cut_based", "mixed",
    "still_photographs", "native_multishot_sequences",
]
SourceCoveragePolicy = Literal["all_catalogued", "approved_subset", "director_selection"]

FAL_KLING_V3_STANDARD_IMAGE_TO_VIDEO = "fal-ai/kling-video/v3/standard/image-to-video"
FAL_KLING_V3_PRO_IMAGE_TO_VIDEO = "fal-ai/kling-video/v3/pro/image-to-video"
FAL_KLING_O3_STANDARD_IMAGE_TO_VIDEO = "fal-ai/kling-video/o3/standard/image-to-video"
FAL_KLING_O3_STANDARD_REFERENCE_TO_VIDEO = "fal-ai/kling-video/o3/standard/reference-to-video"
FAL_VEO31_FAST_FIRST_LAST_FRAME_TO_VIDEO = "fal-ai/veo3.1/fast/first-last-frame-to-video"
FAL_GEMINI_OMNI_FLASH_V11_IMAGE_TO_VIDEO = "google/gemini-omni-flash/v1.1/image-to-video"
FAL_MINIMAX_H3_MAX_IMAGE_TO_VIDEO = "minimax/h3-max/image-to-video"
LOCAL_STILL_HOLD_ROUTE = "local/still-hold"


class StrictModel(BaseModel):
    """Shared strict base so misspelled contract fields cannot be ignored."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True,
                              allow_inf_nan=False)


class SourceImage(StrictModel):
    """A stable source ID paired with a source-root-relative local path."""

    source_id: StableId
    local_path: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def relative_posix_path(self) -> "SourceImage":
        """Reject traversal and platform-specific absolute paths before I/O."""
        value = self.local_path
        posix = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if ("\\" in value or posix.is_absolute() or windows.is_absolute()
                or value in {"", "."} or any(part in {"", ".", ".."} for part in posix.parts)):
            raise ValueError("local_path must be a non-traversing relative POSIX path")
        return self


class ShotSource(StrictModel):
    """Use one source in a shot as its start, end, or supporting reference."""

    source_id: StableId
    role: SourceRole


class VisiblePerson(StrictModel):
    """An opaque person ID with visual cues, never an inferred family relationship."""

    person_id: StableId
    visual_cues: Text


class ContactConstraint(StrictModel):
    """A visible contact/support requirement involving declared people only."""

    participant_ids: list[StableId] = Field(min_length=2, max_length=8)
    requirement: Text

    @model_validator(mode="after")
    def unique_participants(self) -> "ContactConstraint":
        if len(set(self.participant_ids)) != len(self.participant_ids):
            raise ValueError("contact participant_ids must be unique")
        return self


class IntentionalAgeChange(StrictModel):
    """An allowed life-stage change for one declared person, if any."""

    person_id: StableId
    description: Text


class CharacterElement(StrictModel):
    """One person-specific provider element, identified without kinship claims.

    An adapter emits this as one Kling v3 ``elements`` item.  ``element_id``
    makes the group stable in plan data; the provider token is determined only
    by the group's position in :attr:`FamilyShot.character_elements`.
    """

    element_id: StableId
    person_id: StableId
    frontal_source_id: StableId
    supporting_reference_source_ids: list[StableId] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_supporting_sources(self) -> "CharacterElement":
        if len(set(self.supporting_reference_source_ids)) != len(self.supporting_reference_source_ids):
            raise ValueError("supporting_reference_source_ids must be unique")
        if self.frontal_source_id in self.supporting_reference_source_ids:
            raise ValueError("frontal_source_id cannot also be a supporting reference")
        return self


class AllowedEvent(StrictModel):
    """A permitted event for one visible person over an interval in generated time."""

    person_id: StableId
    description: Text
    start_seconds: float = Field(ge=0, le=600)
    end_seconds: float = Field(gt=0, le=600)

    @model_validator(mode="after")
    def ordered_finite_interval(self) -> "AllowedEvent":
        if not math.isfinite(self.start_seconds) or not math.isfinite(self.end_seconds):
            raise ValueError("allowed-event times must be finite")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("allowed-event end_seconds must follow start_seconds")
        return self


class ShotTiming(StrictModel):
    """Independent generation and intended edit/screen durations, each with a purpose.

    Screen time may exceed generation time when a later editor deliberately
    creates a hold or slow-motion treatment.  This planning contract does not
    decide whether the editor can execute that treatment.
    """

    generation_duration_seconds: float = Field(gt=0, le=600)
    generation_duration_reason: Text
    screen_duration_seconds: float = Field(gt=0, le=600)
    screen_duration_reason: Text

    @model_validator(mode="after")
    def coherent_durations(self) -> "ShotTiming":
        values = (self.generation_duration_seconds, self.screen_duration_seconds)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("durations must be finite")
        return self


class ContinuousMotionContract(StrictModel):
    """Required physical and camera-motion facts for a continuous bridge.

    Direction and momentum are deliberately small structural vocabularies so
    adjacent bridges can be checked mechanically.  The prose fields retain the
    director's description; this model does not claim it can compare prose.
    """

    meaningful_physical_or_environment_motion: Text
    camera_path: Text
    entry_direction: MotionDirection
    exit_direction: MotionDirection
    entry_momentum: MomentumState
    exit_momentum: MomentumState
    motivated_handoff_device: Text
    static_fallback_forbidden: Literal[True]


class NormalSpeedSelection(StrictModel):
    """Absolute generated-time endpoints selected for the final edit."""

    trim_in_seconds: float = Field(ge=0, le=600)
    trim_out_seconds: float = Field(ge=0, le=600)
    speed_change_forbidden: Literal[True]

    @model_validator(mode="after")
    def ordered_finite_endpoints(self) -> "NormalSpeedSelection":
        if not all(math.isfinite(value) for value in (
            self.trim_in_seconds, self.trim_out_seconds,
        )):
            raise ValueError("normal-speed selection endpoints must be finite")
        if self.trim_out_seconds <= self.trim_in_seconds:
            raise ValueError("trim_out_seconds must follow trim_in_seconds")
        return self


class MotionHandleRange(StrictModel):
    """A generated-time handle deliberately retained at a moving boundary."""

    start_seconds: float = Field(ge=0, le=600)
    end_seconds: float = Field(gt=0, le=600)

    @model_validator(mode="after")
    def ordered_finite_range(self) -> "MotionHandleRange":
        if not all(math.isfinite(value) for value in (self.start_seconds, self.end_seconds)):
            raise ValueError("motion-handle times must be finite")
        if self.end_seconds <= self.start_seconds:
            raise ValueError("motion-handle end_seconds must follow start_seconds")
        return self


class ContinuousJoinBase(StrictModel):
    """Fields shared by every executable moving-handoff strategy."""

    outgoing_shot_id: StableId
    incoming_shot_id: StableId
    shared_anchor_source_id: StableId


class DerivedSourceBinding(StrictModel):
    """Bind a frame extracted from one take to the next generation request."""

    source_id: StableId
    binding: Literal["start_frame"]


class ContinuationFromExtractedFrameJoin(ContinuousJoinBase):
    """Continue from an identified boundary frame produced by the outgoing take."""

    production_strategy: Literal["continuation_from_extracted_frame"]
    outgoing_extraction_seconds: float = Field(ge=0, le=600)
    outgoing_extraction_frame_index: int = Field(ge=0)
    extracted_frame_id: StableId
    derived_source_id: StableId
    incoming_generation_binding: DerivedSourceBinding

    @model_validator(mode="after")
    def finite_extraction_and_matching_binding(self) -> "ContinuationFromExtractedFrameJoin":
        if not math.isfinite(self.outgoing_extraction_seconds):
            raise ValueError("outgoing extraction time must be finite")
        if self.incoming_generation_binding.source_id != self.derived_source_id:
            raise ValueError("incoming generation binding must use derived_source_id")
        if self.derived_source_id == self.shared_anchor_source_id:
            raise ValueError("derived_source_id must identify the extracted frame, not the review anchor")
        return self


class FullFrameOcclusionJoin(ContinuousJoinBase):
    """Hide the complete boundary behind a matching foreground occluder."""

    production_strategy: Literal["full_frame_occlusion"]
    outgoing_occlusion: MotionHandleRange
    incoming_occlusion: MotionHandleRange
    occluder_match_description: Text


class MatchOnActionJoin(ContinuousJoinBase):
    """Join two boundary intervals at the same described action phase."""

    production_strategy: Literal["match_on_action"]
    outgoing_action: MotionHandleRange
    incoming_action: MotionHandleRange
    action_phase_description: Text


class OverlappingMotionHandlesJoin(ContinuousJoinBase):
    """Match overlapping boundary handles through explicit visual/action correspondence."""

    production_strategy: Literal["overlapping_motion_handles"]
    outgoing_handle: MotionHandleRange
    incoming_handle: MotionHandleRange
    visual_action_correspondence: Text


ContinuousJoin = Annotated[
    ContinuationFromExtractedFrameJoin
    | FullFrameOcclusionJoin
    | MatchOnActionJoin
    | OverlappingMotionHandlesJoin,
    Field(discriminator="production_strategy"),
]


class FilmForm(StrictModel):
    """The user-selected edit grammar for a plan revision.

    ``continuous_moving_transitions`` is intentionally stronger than an
    aesthetic preference: it has no hold, dissolve, or visible-cut exception.
    The other forms remain choices the user can make without acquiring the
    continuous form's constraints.
    """

    kind: FilmFormKind
    allow_still_holds: bool = False
    allow_dissolves: bool = False
    allow_visible_cuts: bool = False

    @model_validator(mode="after")
    def continuous_form_has_no_editorial_substitutions(self) -> "FilmForm":
        if self.kind == "continuous_moving_transitions" and any((
            self.allow_still_holds, self.allow_dissolves, self.allow_visible_cuts,
        )):
            raise ValueError("continuous_moving_transitions forbids holds, dissolves, and visible cuts")
        return self


class SourceCoverage(StrictModel):
    """The user-selected source sequence that must participate in this plan.

    For continuous moving transitions, this ordered sequence is the runtime
    chronology: each adjacent pair must be one consecutive bridge.  Other
    film forms retain the same explicit selected-source record without adding
    a forced ordering rule.
    """

    policy: SourceCoveragePolicy
    source_ids: list[StableId] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def unique_source_ids(self) -> "SourceCoverage":
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("source coverage source_ids must be unique")
        return self


class FamilyShot(StrictModel):
    """One explicit generation or hold beat in the family-film edit."""

    shot_id: StableId
    mode: ShotMode
    provider_route: ProviderRoute
    sources: list[ShotSource] = Field(min_length=1, max_length=16)
    visible_people: list[VisiblePerson] = Field(default_factory=list, max_length=16)
    contact_constraints: list[ContactConstraint] = Field(default_factory=list, max_length=16)
    primary_action: Text
    start_state: Text
    end_state: Text
    intentional_age_changes: list[IntentionalAgeChange] = Field(default_factory=list, max_length=16)
    character_elements: list[CharacterElement] = Field(default_factory=list, max_length=16)
    allowed_events: list[AllowedEvent] = Field(default_factory=list, max_length=32)
    timing: ShotTiming
    continuous_motion: ContinuousMotionContract | None = None
    normal_speed_selection: NormalSpeedSelection | None = None

    @model_validator(mode="after")
    def valid_mode_and_people(self) -> "FamilyShot":
        if self.provider_route == LOCAL_STILL_HOLD_ROUTE and self.mode != "still_hold":
            raise ValueError("local/still-hold accepts only still_hold shots")
        if self.mode == "still_hold" and self.provider_route != LOCAL_STILL_HOLD_ROUTE:
            raise ValueError("still_hold must use local/still-hold")
        source_ids = [source.source_id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("a source may have only one role in a shot")
        roles = [source.role for source in self.sources]
        starts, ends, references = roles.count("start"), roles.count("end"), roles.count("reference")
        if self.mode == "still_hold" and (starts, ends, references) != (1, 0, 0):
            raise ValueError("still_hold requires exactly one start source and no end/reference sources")
        if self.mode == "image_action" and (starts != 1 or ends != 0):
            raise ValueError("image_action requires exactly one start source and no end source")
        if self.mode == "continuous_bridge" and (starts, ends) != (1, 1):
            raise ValueError("continuous_bridge requires exactly one start and one end source")
        if self.mode == "reference_scene" and (starts != 1 or ends != 0 or references < 1):
            raise ValueError("reference_scene requires one start, no end, and at least one reference source")

        person_ids = [person.person_id for person in self.visible_people]
        if len(set(person_ids)) != len(person_ids):
            raise ValueError("visible person_ids must be unique")
        known_people = set(person_ids)
        for constraint in self.contact_constraints:
            if not set(constraint.participant_ids) <= known_people:
                raise ValueError("contact constraints may reference only visible_people")
        for age_change in self.intentional_age_changes:
            if age_change.person_id not in known_people:
                raise ValueError("intentional age changes may reference only visible_people")
        element_ids = [element.element_id for element in self.character_elements]
        if len(set(element_ids)) != len(element_ids):
            raise ValueError("character element_ids must be unique")
        element_people = [element.person_id for element in self.character_elements]
        if len(set(element_people)) != len(element_people):
            raise ValueError("each visible person may have only one character element")
        role_by_source = {source.source_id: source.role for source in self.sources}
        for element in self.character_elements:
            if element.person_id not in known_people:
                raise ValueError("character elements may reference only visible_people")
            element_sources = [element.frontal_source_id, *element.supporting_reference_source_ids]
            if any(role_by_source.get(source_id) != "reference" for source_id in element_sources):
                raise ValueError("character element sources must be declared with the reference role")
        for event in self.allowed_events:
            if event.person_id not in known_people:
                raise ValueError("allowed events may reference only visible_people")
            if event.end_seconds > self.timing.generation_duration_seconds:
                raise ValueError("allowed-event interval must fit generation duration")
        return self

    def character_element_token(self, element_id: str) -> str:
        """Return the documented Kling token (``@Element1`` etc.) for an element.

        Adapters should use this only after selecting a route that supports
        character elements.  The method does not assert that any route does.
        """
        for index, element in enumerate(self.character_elements, start=1):
            if element.element_id == element_id:
                return f"@Element{index}"
        raise KeyError(f"unknown character element ID: {element_id}")


class FamilyShotPlan(StrictModel):
    """The complete v1 source catalog and ordered family-film shot list."""

    schema_version: Literal["family-shot-plan/v1"]
    sources: list[SourceImage] = Field(min_length=1, max_length=256)
    shots: list[FamilyShot] = Field(min_length=1, max_length=512)
    film_form: FilmForm | None = None
    source_coverage: SourceCoverage | None = None
    edit_fps: int | None = Field(default=None, ge=1, le=120)
    joins: list[ContinuousJoin] | None = Field(default=None, max_length=511)

    @model_validator(mode="after")
    def consistent_ids_and_references(self) -> "FamilyShotPlan":
        source_ids = [source.source_id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_id values must be unique")
        shot_ids = [shot.shot_id for shot in self.shots]
        if len(set(shot_ids)) != len(shot_ids):
            raise ValueError("shot_id values must be unique")
        known_sources = set(source_ids)
        for shot in self.shots:
            unknown = {source.source_id for source in shot.sources} - known_sources
            if unknown:
                raise ValueError(f"shot {shot.shot_id} references unknown source IDs: {sorted(unknown)}")
        if (self.film_form is None) != (self.source_coverage is None):
            raise ValueError("film_form and source_coverage must be supplied together")
        if self.source_coverage is not None:
            covered = set(self.source_coverage.source_ids)
            if not covered <= known_sources:
                raise ValueError("source coverage contains unknown source IDs")
            used = {source.source_id for shot in self.shots for source in shot.sources}
            if used != known_sources:
                raise ValueError("every catalogued source must participate in a shot")
            timeline_sources = {
                source.source_id
                for shot in self.shots
                for source in shot.sources
                if source.role in {"start", "end"}
            }
            if timeline_sources != covered:
                raise ValueError(
                    "source coverage must contain every start/end source and exclude reference-only assets"
                )
            if self.film_form is not None and self.film_form.kind == "continuous_moving_transitions":
                if len(self.source_coverage.source_ids) < 2:
                    raise ValueError("continuous_moving_transitions requires at least two covered sources")
                substitutions = [shot.shot_id for shot in self.shots if shot.mode != "continuous_bridge"]
                if substitutions:
                    raise ValueError(
                        "continuous_moving_transitions requires every shot to be a continuous_bridge: "
                        + ", ".join(substitutions)
                    )
                padded = [
                    shot.shot_id for shot in self.shots
                    if shot.timing.screen_duration_seconds > shot.timing.generation_duration_seconds
                ]
                if padded:
                    raise ValueError(
                        "continuous_moving_transitions forbids screen-time padding that creates a still hold: "
                        + ", ".join(padded)
                    )
                if timeline_sources != covered:
                    raise ValueError("every covered source must participate in a continuous moving transition")
                expected_pairs = list(zip(
                    self.source_coverage.source_ids,
                    self.source_coverage.source_ids[1:],
                ))
                actual_pairs = [
                    (
                        next(source.source_id for source in shot.sources if source.role == "start"),
                        next(source.source_id for source in shot.sources if source.role == "end"),
                    )
                    for shot in self.shots
                ]
                if actual_pairs != expected_pairs:
                    raise ValueError(
                        "continuous_moving_transitions must follow consecutive ordered source_coverage source_ids"
                    )
                if self.edit_fps is None:
                    raise ValueError("continuous_moving_transitions requires declared edit_fps")
                if self.joins is None or len(self.joins) != len(self.shots) - 1:
                    raise ValueError("continuous_moving_transitions requires exactly N-1 ordered joins")
                for shot in self.shots:
                    if shot.continuous_motion is None:
                        raise ValueError(
                            f"continuous bridge {shot.shot_id} requires a strict continuous_motion contract"
                        )
                    if shot.normal_speed_selection is None:
                        raise ValueError(
                            f"continuous bridge {shot.shot_id} requires normal_speed_selection"
                        )
                    self._validate_normal_speed_selection(shot)
                self._validate_ordered_joins()
        return self

    def _validate_normal_speed_selection(self, shot: FamilyShot) -> None:
        """Check an editor can use one frame-aligned span without retiming it."""
        assert self.edit_fps is not None
        assert shot.normal_speed_selection is not None
        selection = shot.normal_speed_selection
        generation = shot.timing.generation_duration_seconds
        if selection.trim_out_seconds > generation:
            raise ValueError(
                f"continuous bridge {shot.shot_id} selection endpoints must stay inside generation duration"
            )
        selected_span = selection.trim_out_seconds - selection.trim_in_seconds
        if not math.isclose(selected_span, shot.timing.screen_duration_seconds, abs_tol=1e-9):
            raise ValueError(
                f"continuous bridge {shot.shot_id} selected normal-speed span must equal screen_duration_seconds"
            )
        for label, seconds in (
            ("generation duration", generation),
            ("trim_in_seconds", selection.trim_in_seconds),
            ("trim_out_seconds", selection.trim_out_seconds),
            ("screen_duration_seconds", shot.timing.screen_duration_seconds),
        ):
            if not self._is_frame_aligned(seconds):
                raise ValueError(
                    f"continuous bridge {shot.shot_id} {label} must be frame-aligned at edit_fps"
                )

    def _validate_ordered_joins(self) -> None:
        """Check joins name the actual neighbours and expose usable moving handles."""
        assert self.joins is not None
        assert self.edit_fps is not None
        for index, join in enumerate(self.joins):
            outgoing, incoming = self.shots[index], self.shots[index + 1]
            if (join.outgoing_shot_id, join.incoming_shot_id) != (outgoing.shot_id, incoming.shot_id):
                raise ValueError("continuous_moving_transitions joins must name ordered adjacent shots")
            outgoing_end = next(source.source_id for source in outgoing.sources if source.role == "end")
            incoming_start = next(source.source_id for source in incoming.sources if source.role == "start")
            if (outgoing_end != incoming_start or join.shared_anchor_source_id != outgoing_end):
                raise ValueError("continuous_moving_transitions join shared anchor must match adjacent shots")
            assert outgoing.continuous_motion is not None
            assert incoming.continuous_motion is not None
            if outgoing.continuous_motion.exit_direction != incoming.continuous_motion.entry_direction:
                raise ValueError("continuous_moving_transitions join direction mismatch")
            if outgoing.continuous_motion.exit_momentum != incoming.continuous_motion.entry_momentum:
                raise ValueError("continuous_moving_transitions join momentum mismatch")
            if isinstance(join, OverlappingMotionHandlesJoin):
                self._validate_boundary_range(outgoing, join.outgoing_handle, outgoing=True)
                self._validate_boundary_range(incoming, join.incoming_handle, outgoing=False)
            elif isinstance(join, FullFrameOcclusionJoin):
                self._validate_boundary_range(outgoing, join.outgoing_occlusion, outgoing=True)
                self._validate_boundary_range(incoming, join.incoming_occlusion, outgoing=False)
            elif isinstance(join, MatchOnActionJoin):
                self._validate_boundary_range(outgoing, join.outgoing_action, outgoing=True)
                self._validate_boundary_range(incoming, join.incoming_action, outgoing=False)
            else:
                self._validate_extracted_frame_join(outgoing, incoming, join)

    def _validate_boundary_range(
        self, shot: FamilyShot, handle: MotionHandleRange, *, outgoing: bool,
    ) -> None:
        """Keep a frame-aligned strategy interval at the selected boundary."""
        assert shot.normal_speed_selection is not None
        selection = shot.normal_speed_selection
        selected_start = selection.trim_in_seconds
        selected_end = selection.trim_out_seconds
        if not (selected_start <= handle.start_seconds < handle.end_seconds <= selected_end):
            raise ValueError(f"continuous bridge {shot.shot_id} join interval must stay inside selected span")
        for seconds in (handle.start_seconds, handle.end_seconds):
            if not self._is_frame_aligned(seconds):
                raise ValueError(f"continuous bridge {shot.shot_id} join interval must be frame-aligned at edit_fps")
        boundary = handle.end_seconds if outgoing else handle.start_seconds
        expected = selected_end if outgoing else selected_start
        if not math.isclose(boundary, expected, abs_tol=1e-9):
            which = "outgoing" if outgoing else "incoming"
            raise ValueError(f"continuous bridge {shot.shot_id} {which} join interval must reach selected boundary")

    def _validate_extracted_frame_join(
        self,
        outgoing: FamilyShot,
        incoming: FamilyShot,
        join: ContinuationFromExtractedFrameJoin,
    ) -> None:
        """Bind the last selected outgoing frame to the first selected incoming frame.

        Edit ranges use FFmpeg's half-open convention: ``[trim_in, trim_out)``.
        The visual boundary is therefore the outgoing frame immediately before
        ``trim_out``.  Because that extracted image becomes provider frame zero,
        an extracted-frame continuation cannot discard an incoming head handle.
        """
        assert self.edit_fps is not None
        assert outgoing.normal_speed_selection is not None
        assert incoming.normal_speed_selection is not None
        extraction = join.outgoing_extraction_seconds
        if not self._is_frame_aligned(extraction):
            raise ValueError("continuation extraction time must be frame-aligned at edit_fps")
        selected_end_exclusive = round(
            outgoing.normal_speed_selection.trim_out_seconds * self.edit_fps
        )
        expected_frame = selected_end_exclusive - 1
        expected_extraction = expected_frame / self.edit_fps
        if not math.isclose(extraction, expected_extraction, abs_tol=1e-9):
            raise ValueError(
                "continuation extraction must identify the last frame in the half-open outgoing selection"
            )
        if join.outgoing_extraction_frame_index != expected_frame:
            raise ValueError("continuation frame index must match extraction time at edit_fps")
        if not math.isclose(
            incoming.normal_speed_selection.trim_in_seconds, 0.0, abs_tol=1e-9,
        ):
            raise ValueError(
                "continuation incoming selection must start at provider frame zero"
            )

    def _is_frame_aligned(self, seconds: float) -> bool:
        assert self.edit_fps is not None
        return math.isclose(seconds * self.edit_fps, round(seconds * self.edit_fps), abs_tol=1e-9)


def source_path_below(source_root: Path, local_path: str) -> Path:
    """Resolve one source and require its real path to stay under ``source_root``.

    Resolving both paths is intentional: a source-root-internal symlink that
    points outside the root is rejected before Pillow reads it.
    """
    root = Path(source_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"source_root is not a directory: {source_root}")
    candidate = (root / local_path).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("source path resolves outside source_root") from exc
    if not candidate.is_file():
        raise ValueError(f"source path is not a regular file: {local_path}")
    return candidate
