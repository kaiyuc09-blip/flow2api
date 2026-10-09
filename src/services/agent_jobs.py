"""Durable Agent jobs around the existing generation pipeline.

One service worker owns execution. A lost HTTP connection never cancels a job;
after a process restart unfinished work is UNKNOWN and is never resubmitted.
"""

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

import aiosqlite

from .generation_policy import (reset_no_submit_retry, set_no_submit_retry,
                                reset_native_credit_limit, set_native_credit_limit)
from .flow_current import start_captcha_call_observation, reset_captcha_call_observation


class RequestConflict(ValueError):
    pass


class QueueFull(RuntimeError):
    pass


def _failure(code, message):
    return {"code": code, "message": message, "retryable": False}


def _delivery_warnings(payload):
    messages = {
        "cache_failed": "Media caching failed; delivery may use an expiring URL or inline image.",
        "image_upsample_failed": "Image upscaling failed; the requested resolution was not delivered.",
        "video_upsample_failed": "Video upscaling failed; the requested resolution was not delivered.",
        "image_upsample_outcome_unknown": "Image upscaling may have been accepted upstream. The original image is delivered; do not retry upscaling automatically.",
        "video_upsample_outcome_unknown": "Video upscaling may have been accepted upstream. The original video is delivered; do not retry upscaling automatically.",
    }
    warnings = []
    for warning in payload.get("warnings") or []:
        code = warning.get("code") if isinstance(warning, dict) else None
        if code not in messages:
            code = "upstream_warning"
        if not any(item["code"] == code for item in warnings):
            warnings.append({"code": code, "message": messages.get(code, "The upstream reported a delivery warning.")})
    if payload.get("degraded") and not warnings:
        warnings.append({"code": "degraded_delivery", "message": "The requested output was degraded."})
    return warnings


def _classify_failure(error):
    # Never return upstream exception text: it can contain cookies or URLs.
    if isinstance(error, dict) and error.get("outcome_unknown"):
        return "unknown", _failure("upstream_outcome_unknown", "The upstream may have accepted the generation. Do not resubmit automatically; check the existing request in Flow.")
    if isinstance(error, dict) and error.get("code") == "native_credit_limit":
        cost, limit = error.get("credits_shown"), error.get("max_credits")
        if type(cost) is int and type(limit) is int and 0 <= limit <= 1000 and cost > limit:
            return "failed", {
                **_failure("native_credit_limit", "Flow's displayed cost exceeds this request's budget. Nothing was submitted. Ask the user before creating a new request with a higher limit."),
                "credits_shown": cost, "max_credits": limit, "submission_started": False,
            }
    text = str(error).lower()
    if any(word in text for word in ("timeout", "timed out", "超时")):
        return "unknown", _failure("upstream_timeout", "Generation timed out; its upstream outcome is unknown. Do not resubmit automatically.")
    if any(word in text for word in ("captcha", "验证码", "打码")):
        return "failed", _failure("captcha_failed", "Flow verification failed. Check the configured verification service.")
    if any(word in text for word in ("credit", "quota", "余额", "积分")):
        return "failed", _failure("quota_unavailable", "The account has insufficient quota or is rate limited.")
    if any(word in text for word in ("token", "cookie", "credential", "unauthor", "过期", "账号")):
        return "failed", _failure("account_unavailable", "A usable Flow account or session is required.")
    return "failed", _failure("upstream_generation_failed", "Flow did not complete the generation. Check the account and service status before retrying.")


class AgentJobManager:
    """Persist results and idempotency separately from upstream operation IDs."""

    def __init__(self, db_path, handler, max_pending=8):
        self.db_path = str(db_path)
        self.handler = handler
        self.max_pending = max_pending
        self._tasks = set()
        self._submit_lock = asyncio.Lock()
        self._accepting = False

    async def start(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS agent_generations (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL, model TEXT NOT NULL,
                status TEXT NOT NULL, result_json TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                max_credits INTEGER NOT NULL DEFAULT 0,
                captcha_call_count INTEGER
            )""")
            columns = await (await db.execute("PRAGMA table_info(agent_generations)")).fetchall()
            if "max_credits" not in {column[1] for column in columns}:
                await db.execute("ALTER TABLE agent_generations ADD COLUMN max_credits INTEGER NOT NULL DEFAULT 0")
            if "captcha_call_count" not in {column[1] for column in columns}:
                await db.execute("ALTER TABLE agent_generations ADD COLUMN captcha_call_count INTEGER")
            interrupted = json.dumps({"media": [], "warnings": [], "error": _failure(
                "service_restarted", "Service stopped before the result was recorded. The upstream outcome is unknown; do not resubmit automatically.")})
            await db.execute(
                "UPDATE agent_generations SET status='unknown', result_json=?, captcha_call_count=NULL, updated_at=? WHERE status IN ('queued','running')",
                (interrupted, time.time()),
            )
            await db.commit()
        self._accepting = True

    async def get(self, job_id):
        return await self._find("id", job_id)

    async def get_by_request_id(self, request_id):
        return await self._find("request_id", request_id)

    async def _find(self, column, value):
        assert column in ("id", "request_id")
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(f"SELECT * FROM agent_generations WHERE {column}=?", (value,))
            row = await cursor.fetchone()
        if not row:
            return None
        result = json.loads(row["result_json"])
        return {
            "id": row["id"], "request_id": row["request_id"],
            "model": row["model"], "status": row["status"],
            "max_credits": row["max_credits"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "media": [], "warnings": [], "error": None,
            "upstream_model_verified": False, **result,
            "captcha_call_count": row["captcha_call_count"],
        }

    @staticmethod
    def _request_hash(model, prompt, images, max_credits=0):
        digest = hashlib.sha256()
        digest.update(json.dumps([model, prompt], ensure_ascii=False).encode())
        for data in images:
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        if max_credits:
            digest.update(b"\0credit_limit:")
            digest.update(str(max_credits).encode())
        return digest.hexdigest()

    async def submit(self, model, prompt, images, request_id, base_url=None, max_credits=0):
        if type(max_credits) is not int or not 0 <= max_credits <= 1000:
            raise ValueError("Invalid native credit limit")
        request_hash = self._request_hash(model, prompt, images, max_credits)
        async with self._submit_lock:
            if not self._accepting:
                raise QueueFull("Generation service is not accepting new jobs")
            async with aiosqlite.connect(self.db_path, timeout=30) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT id,request_hash FROM agent_generations WHERE request_id=?", (request_id,))
                old = await cursor.fetchone()
                if old:
                    if old[1] != request_hash:
                        raise RequestConflict("request_id was already used with different generation parameters")
                    await db.rollback()
                    return await self.get(old[0])
                if len(self._tasks) >= self.max_pending:
                    raise QueueFull("Generation queue is full; retry the same request_id later")
                job_id, now = str(uuid.uuid4()), time.time()
                await db.execute(
                    "INSERT INTO agent_generations (id,request_id,request_hash,model,status,result_json,created_at,updated_at,max_credits) VALUES (?,?,?,?,?,?,?,?,?)",
                    (job_id, request_id, request_hash, model, "queued", "{}", now, now, max_credits),
                )
                await db.commit()
            task = asyncio.create_task(self._run(job_id, model, prompt, list(images), base_url, max_credits))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return await self.get(job_id)

    async def _record_captcha_call(self, job_id, count):
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute(
                "UPDATE agent_generations SET captcha_call_count=MAX(COALESCE(captcha_call_count,0),?), updated_at=? WHERE id=?",
                (count, time.time(), job_id),
            )
            await db.commit()

    async def _save(self, job_id, status, result=None, captcha_call_count=None):
        async with aiosqlite.connect(self.db_path, timeout=30) as db:
            await db.execute(
                "UPDATE agent_generations SET status=?, result_json=?, captcha_call_count=?, updated_at=? WHERE id=?",
                (status, json.dumps(result or {}, ensure_ascii=False), captcha_call_count, time.time(), job_id),
            )
            await db.commit()

    async def _run(self, job_id, model, prompt, images, base_url, max_credits=0):
        completed = None
        policy_token = set_no_submit_retry(True)
        credit_token = set_native_credit_limit(max_credits)
        observation, observation_token = start_captcha_call_observation(
            lambda count: self._record_captcha_call(job_id, count)
        )

        async def save(status, result=None):
            await observation.flush()
            await self._save(job_id, status, result, captcha_call_count=observation.count)

        try:
            await save("running")
            error = None
            async for chunk in self.handler.handle_generation(
                model=model, prompt=prompt, images=images or None, stream=False,
                base_url_override=base_url, preserve_parameters=True,
            ):
                payload = json.loads(chunk) if isinstance(chunk, str) else chunk
                if not isinstance(payload, dict):
                    continue
                if payload.get("error"):
                    error = payload["error"]
                media = payload.get("media")
                if isinstance(media, list) and media and all(
                    isinstance(item, dict) and item.get("url") and item.get("type") in ("image", "video")
                    for item in media
                ):
                    completed = {
                        "media": media, "error": None,
                        "requested_model": model,
                        "resolved_model": payload.get("resolved_model", model),
                        "actual_upstream_model": payload.get("actual_upstream_model", "unknown"),
                        "upstream_model_verified": False,
                        "degraded": bool(payload.get("degraded")),
                        "warnings": _delivery_warnings(payload),
                        "generation_transport": payload.get("generation_transport"),
                        "native_settings": payload.get("native_settings"),
                    }
            if completed:
                if error:
                    completed["warnings"].append({"code": "bookkeeping_failed", "message": "The media was generated, but subsequent bookkeeping reported an error."})
                await save("completed", completed)
            elif error:
                status, public_error = _classify_failure(error)
                await save(status, {"error": public_error})
            else:
                await save("unknown", {"error": _failure(
                    "missing_media_result", "No structured media result was recorded. Do not resubmit automatically.")})
        except asyncio.CancelledError:
            await save("completed" if completed else "unknown", completed or {
                "error": _failure("execution_interrupted", "Local processing stopped; the upstream outcome is unknown. Do not resubmit automatically.")})
            raise
        except Exception:
            # Exceptions outside an explicit upstream rejection may occur after
            # submission; do not claim a safe-to-retry failure.
            await save("completed" if completed else "unknown", completed or {
                "error": _failure("execution_outcome_unknown", "The result could not be recorded. Check Flow before submitting another generation.")})
        finally:
            reset_captcha_call_observation(observation_token)
            reset_native_credit_limit(credit_token)
            reset_no_submit_retry(policy_token)

    async def close(self):
        async with self._submit_lock:
            self._accepting = False
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
