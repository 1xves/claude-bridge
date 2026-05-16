"""
Claude Bridge — FastAPI service
────────────────────────────────
Public HTTPS endpoint that lets ChatGPT Custom GPT Actions call Claude
and trigger adversarial review sessions. Deploy on Railway or Render.

ENDPOINTS:
  GET  /health          Liveness check. Returns key config status.
  POST /ask             Ask Claude a single question.
  POST /adversarial     Run a full adversarial session (Claude + OpenAI).
  GET  /usage           Usage summary for the last N days.

AUTH:
  All POST endpoints require:
    Authorization: Bearer <BRIDGE_API_KEY>

  Set BRIDGE_API_KEY in your Railway/Render environment variables.
  The Custom GPT Action sends this header automatically once configured.

ENVIRONMENT VARIABLES (set in Railway/Render dashboard):
  BRIDGE_API_KEY            (required) Secret token for authenticating callers.
  ANTHROPIC_API_KEY         (required) Your Anthropic API key.
  ANTHROPIC_DEFAULT_MODEL   (optional) Default Claude model. Default: claude-opus-4-6.
  OPENAI_API_KEY            (add when ready) OpenAI key for adversarial sessions.
  OPENAI_DEFAULT_MODEL      (optional) Default OpenAI model. Default: gpt-4o.
  ROLES_DIR                 (optional) Path to adversarial-ai/roles directory.
                            Default: ../adversarial-ai/roles relative to this file.
  BRIDGE_LOG_DB             (optional) Path for the SQLite usage log.
                            Default: usage.db in the same directory as this file.
  PORT                      (injected by Railway automatically)

DEPLOY TO RAILWAY:
  1. Push this project to a GitHub repo.
  2. Create a new Railway project → Deploy from GitHub repo.
  3. Set the environment variables above in Railway's Variables tab.
  4. Railway auto-detects the Python app and runs: uvicorn server:app --host 0.0.0.0 --port $PORT
  5. Copy the generated Railway URL — you'll put it in the Custom GPT Action.
"""

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from anthropic import (
    Anthropic,
    APIError             as AntAPIError,
    AuthenticationError  as AntAuthError,
    RateLimitError       as AntRateLimitError,
    BadRequestError      as AntBadRequestError,
)

# Logger lives alongside this file
sys.path.insert(0, str(Path(__file__).parent))
from logger import CallLogger, init_db, usage_summary

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Claude Bridge",
    version="1.0.0",
    description="Proxy service that lets ChatGPT Custom GPTs call Claude and run adversarial reviews.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chat.openai.com", "https://chatgpt.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# Initialize usage log DB on startup
@app.on_event("startup")
def on_startup():
    try:
        init_db()
    except Exception as exc:
        print(f"Warning: could not initialise usage log DB: {exc}", file=sys.stderr)

# ── Auth ──────────────────────────────────────────────────────────────────────
_http_bearer = HTTPBearer()

def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(_http_bearer),
) -> str:
    """Validate the Bearer token and return the caller identity."""
    expected = os.environ.get("BRIDGE_API_KEY")
    if not expected:
        raise HTTPException(status_code=500, detail="BRIDGE_API_KEY not configured on server.")
    if credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="Invalid API key.")
    # Use the token itself as caller_id (all callers share one key for now).
    # Phase 5 upgrade: issue per-user tokens and return the username here.
    return "workspace"

# ── Anthropic client ──────────────────────────────────────────────────────────
_anthropic_client: Optional[Anthropic] = None

def _get_anthropic() -> Anthropic:
    global _anthropic_client
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY not configured on server.")
    if _anthropic_client is None:
        _anthropic_client = Anthropic(api_key=key)
    return _anthropic_client

def _default_claude_model() -> str:
    return os.environ.get("ANTHROPIC_DEFAULT_MODEL", "claude-opus-4-6")

def _default_openai_model() -> str:
    return os.environ.get("OPENAI_DEFAULT_MODEL", "gpt-4o")

# ── OpenAI client (lazy, optional until key is added) ────────────────────────
_openai_client = None

def _get_openai():
    global _openai_client
    key = os.environ.get("OPENAI_API_KEY")
    if not key or key.startswith("PLACEHOLDER"):
        raise HTTPException(
            status_code=503,
            detail=(
                "OPENAI_API_KEY is not configured yet. "
                "Add your key to the Railway environment variables to enable "
                "adversarial sessions and cross-model calls."
            ),
        )
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=key, timeout=180.0)
    return _openai_client

# ── Role prompt loading ───────────────────────────────────────────────────────
def _roles_dir() -> Path:
    env = os.environ.get("ROLES_DIR")
    if env:
        return Path(env)
    return Path(__file__).parent.parent / "adversarial-ai" / "roles"

def _load_role(role: str, domain: str) -> str:
    path = _roles_dir() / role / f"{domain}.md"
    if not path.exists():
        raise HTTPException(
            status_code=500,
            detail=(
                f"Role prompt not found: {path}. "
                "Set the ROLES_DIR environment variable to the adversarial-ai/roles directory."
            ),
        )
    return path.read_text(encoding="utf-8")

# ── Low-level callers ─────────────────────────────────────────────────────────
def _claude(system: str, messages: list[dict], model: str, call_logger: CallLogger) -> str:
    client = _get_anthropic()
    try:
        resp = client.messages.create(
            model=model, max_tokens=4096, system=system, messages=messages,
        )
    except AntAuthError as e:
        raise HTTPException(status_code=401, detail=f"Anthropic auth failed: {e}")
    except AntRateLimitError as e:
        raise HTTPException(status_code=429, detail=f"Anthropic rate limit: {e}")
    except AntBadRequestError as e:
        raise HTTPException(status_code=400, detail=f"Anthropic bad request: {e}")
    except AntAPIError as e:
        raise HTTPException(status_code=502, detail=f"Anthropic API error: {e}")
    if resp.usage:
        call_logger.anthropic_tokens(
            input=resp.usage.input_tokens,
            output=resp.usage.output_tokens,
        )
    return resp.content[0].text

def _oai(system: str, messages: list[dict], model: str, call_logger: CallLogger) -> str:
    from openai import OpenAIError, BadRequestError as OAIBadRequest
    client = _get_openai()
    bare = model.lower().split("/")[-1]
    o_series = any(bare.startswith(p) for p in ("o1", "o3", "o4"))
    fmt = [{"role": "system", "content": system}] + messages
    params: dict = {"model": model, "messages": fmt}
    if o_series:
        params["max_completion_tokens"] = 4096
    else:
        params["temperature"] = 0.7
        params["max_tokens"] = 4096
    try:
        resp = client.chat.completions.create(**params)
    except OAIBadRequest as e:
        raise HTTPException(status_code=400, detail=f"OpenAI bad request: {e}")
    except OpenAIError as e:
        raise HTTPException(status_code=502, detail=f"OpenAI API error: {e}")
    if resp.usage:
        call_logger.openai_tokens(
            input=resp.usage.prompt_tokens,
            output=resp.usage.completion_tokens,
        )
    content = resp.choices[0].message.content
    if content is None:
        raise HTTPException(status_code=502, detail="OpenAI returned empty response.")
    return content

def _call_model(
    backend: str, model: str, system: str,
    messages: list[dict], call_logger: CallLogger,
) -> str:
    if backend == "claude":
        return _claude(system, messages, model, call_logger)
    else:
        return _oai(system, messages, model, call_logger)

# ── Request / response models ─────────────────────────────────────────────────

class AskRequest(BaseModel):
    prompt:        str = Field(..., description="The question or task to send to Claude.")
    model:         Optional[str] = Field(None, description="Claude model name. Defaults to ANTHROPIC_DEFAULT_MODEL.")
    system_prompt: Optional[str] = Field(None, description="Optional system framing.")
    max_tokens:    int = Field(2048, description="Max response tokens.")

class AskResponse(BaseModel):
    response: str
    model:    str

class AdversarialRequest(BaseModel):
    task:              str = Field(..., description="The task or artifact to review.")
    domain:            str = Field(..., description="design | engineering | analysis | code-review")
    proposer:          str = Field("claude",  description="'claude' or 'openai'")
    challenger:        str = Field("openai",  description="'claude' or 'openai'")
    proposer_model:    Optional[str] = Field(None)
    challenger_model:  Optional[str] = Field(None)
    rounds:            int = Field(2, description="Critique-defense cycles. 1–5.")
    show_transcript:   bool = Field(True, description="Include full debate transcript in response.")

class AdversarialResponse(BaseModel):
    verdict:       str
    final_proposal: str
    transcript:    Optional[str] = None
    domain:        str
    proposer:      str
    challenger:    str
    rounds:        int

class UsageResponse(BaseModel):
    period_days:              int
    total_calls:              int
    successful_calls:         int
    total_estimated_cost_usd: float
    by_caller:                dict
    by_endpoint:              dict
    by_domain:                dict

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Liveness check. Shows configuration status without exposing key values."""
    ant_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    oai_key = os.environ.get("OPENAI_API_KEY", "")
    oai_ready = bool(oai_key) and not oai_key.startswith("PLACEHOLDER")
    roles_ok  = (_roles_dir() / "proposer" / "design.md").exists()
    return {
        "status":              "ok",
        "anthropic_configured": ant_key,
        "openai_configured":    oai_ready,
        "role_prompts_found":   roles_ok,
        "roles_dir":           str(_roles_dir()),
    }


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest, caller: str = Depends(verify_token)):
    """Ask Claude a single question and return the response."""
    model = request.model or _default_claude_model()
    system = request.system_prompt or "You are a helpful, precise assistant."

    with CallLogger(endpoint="/ask", caller_id=caller, proposer_model=model,
                    proposer_backend="claude") as call_log:
        response_text = _claude(
            system=system,
            messages=[{"role": "user", "content": request.prompt}],
            model=model,
            call_logger=call_log,
        )

    return AskResponse(response=response_text, model=model)


@app.post("/adversarial", response_model=AdversarialResponse)
def adversarial(request: AdversarialRequest, caller: str = Depends(verify_token)):
    """
    Run a full adversarial review session.

    One model proposes, the other challenges, they exchange for N rounds,
    then the Challenger delivers a final verdict.

    NOTE: This may take 2–5 minutes for multi-round sessions — this is normal.
    """
    valid_domains = {"design", "engineering", "analysis", "code-review"}
    if request.domain not in valid_domains:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain '{request.domain}'. Must be one of: {sorted(valid_domains)}",
        )
    if request.proposer not in {"claude", "openai"} or request.challenger not in {"claude", "openai"}:
        raise HTTPException(status_code=400, detail="proposer and challenger must be 'claude' or 'openai'.")
    if not 1 <= request.rounds <= 5:
        raise HTTPException(status_code=400, detail="rounds must be between 1 and 5.")

    p_model = request.proposer_model or (
        _default_claude_model() if request.proposer == "claude" else _default_openai_model()
    )
    c_model = request.challenger_model or (
        _default_claude_model() if request.challenger == "claude" else _default_openai_model()
    )

    proposer_system   = _load_role("proposer",   request.domain)
    challenger_system = _load_role("challenger",  request.domain)

    with CallLogger(
        endpoint="/adversarial",
        caller_id=caller,
        domain=request.domain,
        proposer_backend=request.proposer,
        proposer_model=p_model,
        challenger_backend=request.challenger,
        challenger_model=c_model,
        rounds=request.rounds,
    ) as call_log:

        def call_p(msgs):
            return _call_model(request.proposer, p_model, proposer_system, msgs, call_log)

        def call_c(msgs):
            return _call_model(request.challenger, c_model, challenger_system, msgs, call_log)

        transcript_parts: list[str] = []

        # Initial proposal
        proposal = call_p([{"role": "user", "content": request.task}])
        transcript_parts.append(f"## Proposer — Initial Output\n\n{proposal}")
        current = proposal

        # Rounds
        for rnd in range(1, request.rounds + 1):
            critique = call_c([{"role": "user", "content": (
                f"Proposal to critique:\n\n---\n{current}\n---\n\n"
                "Apply all attack vectors systematically."
            )}])
            transcript_parts.append(f"## Challenger — Round {rnd} Critique\n\n{critique}")

            defense = call_p([
                {"role": "user",      "content": request.task},
                {"role": "assistant", "content": current},
                {"role": "user",      "content": (
                    f"Critique:\n\n---\n{critique}\n---\n\n"
                    "For each point: ACCEPT AND REVISE, DEFEND WITH REASONING, "
                    "or ACKNOWLEDGE AS KNOWN TRADEOFF. Then give your full revised proposal."
                )},
            ])
            transcript_parts.append(f"## Proposer — Round {rnd} Defense\n\n{defense}")
            current = defense

        # Final verdict
        verdict = call_c([{"role": "user", "content": (
            f"Task: {request.task}\n\n"
            f"Final proposal:\n\n---\n{current}\n---\n\n"
            "Give your final verdict: RESOLVED FLAWS, OPEN FLAWS (with severity), "
            "ACKNOWLEDGED TRADEOFFS, and OVERALL VERDICT "
            "(APPROVED / APPROVED WITH CONDITIONS / REJECTED)."
        )}])
        transcript_parts.append(f"## Challenger — Final Verdict\n\n{verdict}")

    full_transcript = "\n\n".join(transcript_parts) if request.show_transcript else None

    return AdversarialResponse(
        verdict=verdict,
        final_proposal=current,
        transcript=full_transcript,
        domain=request.domain,
        proposer=f"{request.proposer}/{p_model}",
        challenger=f"{request.challenger}/{c_model}",
        rounds=request.rounds,
    )


@app.get("/usage", response_model=UsageResponse)
def get_usage(days: int = 30, caller: str = Depends(verify_token)):
    """Return usage summary for the last N days. Defaults to 30."""
    if not 1 <= days <= 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365.")
    summary = usage_summary(days=days)
    if "error" in summary:
        raise HTTPException(status_code=500, detail=summary["error"])
    return summary
