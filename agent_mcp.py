"""Flow2API tools for local MCP clients. Run using stdio; stdout is protocol only."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from src.agent_client import AgentClient, AgentClientError, AgentSettings


def create_server(settings=None, *, transport=None, resolver=None):
    server = FastMCP(
        "Flow2API",
        instructions=(
            "Media generation tools only. First list_models and use its exact available model IDs. "
            "Generation may consume the user's account credits: obtain user authorization first. "
            "Use a stable unique request_id per intended generation and retain it after submission. "
            "Submissions return tasks quickly. Query get_generation later; never automatically "
            "resubmit an unknown or timed-out generation. Completed downloads return local_path."
        ),
        log_level="WARNING",
    )

    async def invoke(method, *args):
        try:
            configured = settings or AgentSettings.from_env()
            async with AgentClient(configured, transport=transport, resolver=resolver) as client:
                return await getattr(client, method)(*args)
        except AgentClientError as error:
            return {"status": "error", "error": {"code": error.code, "message": str(error), "retryable": False}}
        except Exception:
            # Never expose transport exceptions, headers, environment values or file contents.
            return {"status": "error", "error": {"code": "tool_error", "message": "The tool could not complete. Keep the existing task/request ID; do not automatically regenerate.", "retryable": False}}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    async def list_models() -> dict:
        """List exact model IDs, capabilities, availability and verification state; no generation."""
        return await invoke("list_models")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def generate_image(model: str, prompt: str, request_id: str, image_paths: list[str] | None = None, max_credits: int = 0) -> dict:
        """Submit text-to-image or reference-image editing; may use account credits. Use a model from list_models and an 8-128 character stable request_id. Native browser mode reads the displayed credit cost before submitting; max_credits defaults to zero and may be raised only with explicit user approval. In third-party RPC mode max_credits caps neither captcha fees nor Flow credits: captcha tasks are charged per provider policy, and uploads/retries can add calls. Agree a separate spending budget before submission. Optional image_paths are absolute local PNG/JPEG/WebP paths, up to 20 MiB each. Returns a task immediately; call get_generation later for captcha_call_count. Never create a new request_id merely to retry unknown results."""
        return await invoke("submit", "image", model, prompt, image_paths or [], request_id, max_credits)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def submit_video(model: str, prompt: str, request_id: str, image_paths: list[str] | None = None, max_credits: int = 0) -> dict:
        """Submit text-to-video or reference-image video; may use account credits. Select exact model, duration and orientation from list_models. A native browser route must verify displayed credits within max_credits (default zero); raise the limit only after explicit user approval. In third-party RPC mode max_credits caps neither captcha fees nor Flow credits: captcha tasks are charged per provider policy, and uploads/retries can add calls. Agree a separate spending budget before submission. image_paths are absolute local images. Returns a task immediately; query get_generation later for captcha_call_count. Preserve request_id and do not automatically resubmit on timeout."""
        return await invoke("submit", "video", model, prompt, image_paths or [], request_id, max_credits)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def get_generation(generation_id: str | None = None, request_id: str | None = None) -> dict:
        """Read a task once using exactly one generation_id or original request_id. Returns captcha_call_count for observed third-party task-creation attempts including retries, or null when unknown; result polling is excluded and the count is not a billing receipt. After submission timeout, recover by request_id without resubmitting; a missing task returns not_found. Completed media downloads into FLOW2API_OUTPUT_DIR using new filenames, never overwriting. Returns local_path and file validation metadata. Pending/unknown tasks are not resubmitted. MP4 validation checks container structure; playback and visual quality still require inspection."""
        return await invoke("get_generation", generation_id, request_id)

    return server


if __name__ == "__main__":
    create_server().run(transport="stdio")
