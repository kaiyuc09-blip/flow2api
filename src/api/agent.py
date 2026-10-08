"""Additive, authenticated async generation API for tool-using Agents."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.auth import AuthManager
from ..services.agent_jobs import QueueFull, RequestConflict
from ..services.model_capabilities import get_model_capabilities, validate_generation_request
from .routes import _load_image_bytes_from_uri


class PrivateValidationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Pydantic's normal response includes the original input; image
                # data and prompts must not be copied into diagnostic responses.
                return JSONResponse(status_code=422, content={"error": {
                    "code": "invalid_request", "message": "Invalid generation parameters.",
                    "fields": [".".join(map(str, error["loc"])) for error in exc.errors()],
                    "retryable": False,
                }})

        return safe_handler


router = APIRouter(prefix="/v1/agent", tags=["Agent"], route_class=PrivateValidationRoute)
bearer = HTTPBearer(auto_error=False)


async def authenticate(credentials=Security(bearer)):
    if credentials is None or not AuthManager.verify_api_key(credentials.credentials):
        raise HTTPException(401, "Invalid API key")


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=16000)
    images: List[str] = Field(default_factory=list, max_length=14)
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), pattern=r"^[A-Za-z0-9._-]{8,128}$")
    max_credits: int = Field(default=0, ge=0, le=1000, strict=True)

    @field_validator("images")
    @classmethod
    def restrict_reference_inputs(cls, images):
        if sum(len(image) for image in images) > 28 * 1024 * 1024:
            raise ValueError("Reference images exceed the aggregate size limit")
        if any(not image.startswith("data:image/") for image in images):
            raise ValueError("Agent references must be image data URLs")
        return images


def jobs(request):
    manager = getattr(request.app.state, "agent_jobs", None)
    if manager is None:
        raise HTTPException(503, "Agent generation service is not initialized")
    return manager


@router.get("/models", dependencies=[Depends(authenticate)])
async def list_models():
    return {
        "object": "list", "data": get_model_capabilities(),
        "availability_note": "Configured protocol capability only; account access and live model identity require separate verification.",
    }


@router.post("/generations", status_code=202, dependencies=[Depends(authenticate)])
async def submit_generation(body: GenerationRequest, request: Request):
    manager = jobs(request)
    try:
        # No account, upload, or captcha request happens before this preflight.
        # Existing IDs must remain recoverable even if service configuration changed.
        # submit() below still compares the complete payload hash before returning them.
        if await manager.get_by_request_id(body.request_id) is None:
            validate_generation_request(body.model, len(body.images))
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": {
            "code": "unsupported_generation", "message": str(exc), "retryable": False,
        }})
    images = [await _load_image_bytes_from_uri(uri) for uri in body.images]
    try:
        return await manager.submit(
            body.model, body.prompt, images, body.request_id,
            base_url=str(request.base_url).rstrip("/"),
            max_credits=body.max_credits,
        )
    except RequestConflict:
        return JSONResponse(status_code=409, content={"error": {
            "code": "request_id_conflict", "message": "Use the same parameters when retrying an existing request_id.", "retryable": False,
        }})
    except QueueFull:
        return JSONResponse(status_code=429, content={"error": {
            "code": "queue_full", "message": "The generation queue is full. Retry later with the same request_id.", "retryable": True,
        }})


@router.get("/generations/by-request/{request_id}", dependencies=[Depends(authenticate)])
async def get_generation_by_request(request_id: str, request: Request):
    result = await jobs(request).get_by_request_id(request_id)
    if result is None:
        raise HTTPException(404, "Generation not found")
    return result


@router.get("/generations/{job_id}", dependencies=[Depends(authenticate)])
async def get_generation(job_id: str, request: Request):
    result = await jobs(request).get(job_id)
    if result is None:
        raise HTTPException(404, "Generation not found")
    return result
