"""Interaction endpoints: chat, editing intent, presets and edit versions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import AppState, get_state
from backend.interaction.models import (
    Answer,
    ConversationMessage,
    EditingIntent,
    EditVersion,
    InteractionResult,
)

router = APIRouter(prefix="/projects/{project_id}", tags=["interaction"])


class MessageRequest(BaseModel):
    """A free-form user message: question, command or editing instruction."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class PresetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: str = Field(min_length=1, max_length=64)


class PresetInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    label: str
    description: str


class PresetListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: str
    items: list[PresetInfo]


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[ConversationMessage]


class VersionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    items: list[EditVersion]


@router.post("/chat", response_model=InteractionResult)
def chat(
    project_id: str,
    request: MessageRequest,
    state: AppState = Depends(get_state),
) -> InteractionResult:
    """Handle one message: question, command or editing instruction (§15)."""
    return state.interaction.handle(project_id, request.text)


@router.post("/ask", response_model=Answer)
def ask(
    project_id: str,
    request: QuestionRequest,
    state: AppState = Depends(get_state),
) -> Answer:
    """Answer a question from stored analysis data.

    Never re-runs analysis (§6). Before the analysis has produced the data, the
    answer says so rather than guessing (§17).
    """
    return state.interaction.ask(project_id, request.question)


@router.get("/intent", response_model=EditingIntent)
def get_intent(project_id: str, state: AppState = Depends(get_state)) -> EditingIntent:
    """The effective editing brief: preset + project settings + instructions."""
    return state.interaction.current_intent(project_id)


@router.post("/intent/preset", response_model=EditingIntent)
def set_preset(
    project_id: str,
    request: PresetRequest,
    state: AppState = Depends(get_state),
) -> EditingIntent:
    """Choose a preset. Instructions already given are preserved (§4)."""
    return state.interaction.set_preset(project_id, request.preset)


@router.post("/intent/reset", response_model=EditingIntent)
def reset_intent(project_id: str, state: AppState = Depends(get_state)) -> EditingIntent:
    """Discard every instruction, returning to the preset alone."""
    return state.interaction.reset_intent(project_id)


@router.get("/chat/history", response_model=HistoryResponse)
def history(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    state: AppState = Depends(get_state),
) -> HistoryResponse:
    items = state.interaction.history(project_id, limit=limit)
    return HistoryResponse(total=len(items), items=items)


@router.get("/edit-versions", response_model=VersionListResponse)
def versions(project_id: str, state: AppState = Depends(get_state)) -> VersionListResponse:
    """Edit history. Restoring one costs no analysis (§19)."""
    items = state.interaction.versions(project_id)
    return VersionListResponse(total=len(items), items=items)


presets_router = APIRouter(tags=["interaction"])


@presets_router.get("/presets", response_model=PresetListResponse)
def list_presets(state: AppState = Depends(get_state)) -> PresetListResponse:
    """Editing presets offered by this installation (§4)."""
    config = state.config
    return PresetListResponse(
        default=config.interaction.default_preset,
        items=[
            PresetInfo(name=name, label=preset.label, description=preset.description)
            for name, preset in sorted(config.presets.items())
        ],
    )


__all__ = ["presets_router", "router"]
