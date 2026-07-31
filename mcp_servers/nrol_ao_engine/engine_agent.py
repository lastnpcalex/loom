"""Minimal in-process Dream tool-call loop (Track A phase 1, custom Python loop).

This is the engine-agent harness (A.7, custom-loop path). It builds an
OpenAI-format message list, registers the engine-side tools (``TOOLS`` from
``mcp_servers/nrol_ao_engine/tools``), calls Dream via
``dream_client.chat_with_tools``, dispatches returned tool calls to local
Python functions, appends tool results, and loops until ``finish_reason=="stop"``
with no tool calls — or the turn cap is hit.

Safety invariants (phase 1):
  - **No commits, no topic mutation, no posterior movement.** The only tool
    wired in phase 1 is ``fetch_article`` (read-only). There is no path here to
    ``framework/pipeline.py``'s update functions, the evidence log, or the
    proposal store. Action tools arrive in later phases and will wrap the
    *existing* commit gates (Loom approval, governance) — never a new path.
  - **Bounded turns.** ``max_turns`` (default 10) aborts a runaway loop. The
    sidecar is single-model; an unbounded loop would hold it indefinitely.
  - **Fail-closed on malformed arguments.** A tool call whose arguments do not
    parse as JSON is retried once (by appending a tool-message error and
    re-asking); a second failure on the same call aborts the run with a
    recorded error rather than silently dropping the call.
  - **In-process.** No second MCP server process is started (A.7 phase-1 rule).
    The operator MCP imports and calls ``run_engine_agent`` directly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any

from . import dream_client
from .tools import TOOLS

DEFAULT_MAX_TURNS = 10
DEFAULT_TEMPERATURE = 0.2
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT = 600.0

# The system prompt is deliberately terse and imperative. DiffusionGemma is a
# diffusion text model, not an instruction-tuned chat model; verbose prompts
# that describe tool *availability* in the abstract ("you have tools…") nudge
# it toward narrating intent ("I would fetch…") or refusing ("I do not have
# access to a tool") instead of emitting the structured tool_call object. A
# short directive that names the action works better. The §6 prompt-
# engineering requirements (multi-paragraph analysis, citation of prior
# evidence/indicators) land in phase 2+ with the deliberation tools.
DEFAULT_SYSTEM_PROMPT = (
    "Call fetch_article with the URL, then answer the user's question."
)


def _resolve_tool_names(
    tool_names: Iterable[str] | None,
    *,
    tool_registry: dict[str, dict] | None = None,
) -> list[str]:
    """Return the exact tool names this agent run is allowed to expose.

    ``None`` preserves the legacy/default behavior: expose every registered
    tool. Stage-specific callers pass a small allow-list so Dream cannot call
    tools outside that stage's role (the mirror-MCP behavior, without starting
    a second MCP process yet).
    """
    registry = tool_registry or TOOLS
    names = list(registry) if tool_names is None else [str(n) for n in tool_names]
    missing = [name for name in names if name not in registry]
    if missing:
        raise KeyError(f"unknown allowed tool(s): {missing}")
    return names


def _tool_specs(
    tool_names: Iterable[str] | None = None,
    *,
    tool_registry: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """OpenAI-format tool specs for the allowed tools in this run."""
    registry = tool_registry or TOOLS
    return [registry[name]["schema"] for name in _resolve_tool_names(tool_names, tool_registry=registry)]


def _dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    tool_registry: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Call a registered tool by name. Raises KeyError if unknown."""
    registry = tool_registry or TOOLS
    entry = registry.get(name)
    if not entry:
        raise KeyError(f"unknown tool: {name}")
    fn = entry["fn"]
    return fn(**arguments)


def _tool_message(tool_call_id: str, content: str | dict | None) -> dict[str, Any]:
    """Build an OpenAI tool-role message carrying a tool result.

    content is stringified for the sidecar (OpenAI tool messages carry a
    string content). Errors are returned as a JSON object with an ``error`` key
    so the model can see what went wrong and decide whether to retry.
    """
    if content is None:
        body = ""
    elif isinstance(content, str):
        body = content
    else:
        body = json.dumps(content, ensure_ascii=True, default=str)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": body}


def _assistant_message_from_dream(result: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the assistant message to append to the running history.

    Dream returns content + tool_calls separately; we feed them back as a
    single assistant message with both fields so the next turn sees the call
    it made and the tool results that follow.
    """
    msg: dict[str, Any] = {"role": "assistant", "content": result.get("content") or ""}
    tool_calls = result.get("tool_calls") or []
    if tool_calls:
        # Keep the raw sidecar tool_calls shape ({id,type,function:{name,arguments:str}})
        msg["tool_calls"] = tool_calls
    return msg


def _process_tool_calls(
    result: dict[str, Any],
    *,
    retried_calls: set[str],
    tool_registry: dict[str, dict] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Parse + dispatch every tool call in a Dream response.

    Returns (tool_messages, failed_call_ids, fatal_error).
      - tool_messages: one tool-role message per successfully-dispatched call
      - failed_call_ids: call ids whose arguments were malformed (caller
        appends a retry error and continues, unless already retried)
      - fatal_error: set when a call was already retried and failed again —
        the run aborts fail-closed rather than dropping the call.
    """
    tool_messages: list[dict[str, Any]] = []
    failed_call_ids: list[dict[str, Any]] = []
    fatal_error: str | None = None

    for call in result.get("tool_calls") or []:
        call_id = str(call.get("id") or f"call_{uuid.uuid4().hex[:8]}")
        fn_obj = call.get("function") if isinstance(call, dict) else {}
        if not isinstance(fn_obj, dict):
            fn_obj = {}
        tool_name = str(fn_obj.get("name") or "")
        raw_args = fn_obj.get("arguments")

        parsed, parse_err = dream_client.parse_tool_args(raw_args)
        if parse_err is not None:
            if call_id in retried_calls:
                # Second failure on the same call — fail closed.
                fatal_error = (
                    f"Tool call {tool_name!r} (id={call_id}) returned "
                    f"malformed arguments twice ({parse_err}); aborting."
                )
                return tool_messages, failed_call_ids, fatal_error
            retried_calls.add(call_id)
            failed_call_ids.append(call_id)
            tool_messages.append(_tool_message(
                call_id,
                {"error": f"arguments did not parse as JSON: {parse_err}. "
                          "Please re-emit the tool call with valid JSON arguments."},
            ))
            continue

        # Dispatch the parsed call.
        try:
            tool_result = _dispatch_tool(tool_name, parsed, tool_registry=tool_registry)
        except KeyError as exc:
            # Unknown tool — tell the model and let it recover.
            tool_messages.append(_tool_message(
                call_id, {"error": f"unknown tool: {exc}"},
            ))
            continue
        except TypeError as exc:
            # Wrong arguments (missing required param, bad type). Retry once.
            if call_id in retried_calls:
                fatal_error = (
                    f"Tool {tool_name!r} (id={call_id}) rejected arguments "
                    f"twice ({exc}); aborting."
                )
                return tool_messages, failed_call_ids, fatal_error
            retried_calls.add(call_id)
            failed_call_ids.append(call_id)
            tool_messages.append(_tool_message(
                call_id,
                {"error": f"tool rejected arguments: {exc}. Re-emit with the "
                          "correct parameters per the tool schema."},
            ))
            continue
        except Exception as exc:
            # Tool execution failed (e.g. fetch error). Surface it as a tool
            # error result; the model can still reason and stop.
            tool_messages.append(_tool_message(
                call_id, {"error": f"{type(exc).__name__}: {str(exc)[:300]}"},
            ))
            continue

        tool_messages.append(_tool_message(call_id, tool_result))

    return tool_messages, failed_call_ids, fatal_error


def run_engine_agent(
    user_prompt: str,
    *,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    model: str | None = None,
    host: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: float = DEFAULT_TIMEOUT,
    force_first_tool_call: bool = False,
    tool_names: Iterable[str] | None = None,
    tool_registry: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Run the engine-agent tool-call loop to completion.

    Returns a trace dict:
        {
          "ok": bool,                 # True if the loop ended on finish_reason=stop
          "final_text": str,          # the model's final stop-turn text (stripped)
          "turns": int,               # number of Dream calls made
          "tool_calls": list[dict],   # every tool call made (name, args, ok)
          "finish_reason": str,       # the last finish_reason seen
          "error": str | None,        # set when ok=False (turn cap / fatal)
        }

    The trace is the audit record (A.5) — it records what the agent decided
    and why, not just "N chars came back." No raw LLM completions are stored
    here (those remain at the provider level if needed).

    ``force_first_tool_call``: when True, the FIRST turn is sent with
    ``tool_choice="required"`` so the model must emit a tool call instead of
    reasoning about whether to call one. DiffusionGemma is a diffusion model,
    not an instruction-tuned chat model; left to ``auto`` it often narrates
    intent ("I would fetch...") rather than calling. After the first turn the
    choice reverts to ``auto`` so the agent can stop when it has enough. Use
    this when the operator knows the first action must be a tool call (e.g. a
    scan that always starts with fetch_article).

    ``tool_names``: optional stage-specific allow-list. When provided, only
    those tools are sent in the OpenAI ``tools`` payload and only those tools
    can be dispatched. This is the in-process equivalent of giving each
    advocate/rebut/jury MCP session a constrained tool surface.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    registry = tool_registry or TOOLS
    allowed_tool_names = _resolve_tool_names(tool_names, tool_registry=registry)
    tools = _tool_specs(allowed_tool_names, tool_registry=registry)
    trace_calls: list[dict[str, Any]] = []
    retried_calls: set[str] = set()
    turns = 0
    final_text = ""
    last_finish = ""
    error: str | None = None

    while turns < max_turns:
        turns += 1
        # Force a tool call on the first turn when requested; let the model
        # choose (auto) on every subsequent turn so it can stop.
        tool_choice = "required" if (force_first_tool_call and turns == 1) else "auto"
        result = dream_client.chat_with_tools(
            messages,
            tools=tools,
            model=model,
            host=host,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            tool_choice=tool_choice,
        )
        last_finish = result.get("finish_reason") or ""
        messages.append(_assistant_message_from_dream(result))

        tool_calls = result.get("tool_calls") or []
        for call in tool_calls:
            fn_obj = call.get("function") if isinstance(call, dict) else {}
            if not isinstance(fn_obj, dict):
                fn_obj = {}
            trace_calls.append({
                "turn": turns,
                "id": str(call.get("id") or ""),
                "name": str(fn_obj.get("name") or ""),
                "arguments": fn_obj.get("arguments"),
            })

        if not tool_calls:
            # No tool calls — either stop, length, or an empty response.
            final_text = result.get("content") or ""
            if last_finish == "stop":
                break
            # Non-stop finish with no tool calls (e.g. length) — end here with
            # whatever content we have rather than spinning.
            error = error or f"ended with finish_reason={last_finish!r} and no tool calls"
            break

        tool_messages, _failed, fatal = _process_tool_calls(
            result,
            retried_calls=retried_calls,
            tool_registry={name: registry[name] for name in allowed_tool_names},
        )
        if fatal:
            error = fatal
            break
        messages.extend(tool_messages)
    else:
        # Loop exhausted without break (hit max_turns).
        error = f"exceeded max_turns={max_turns} without finishing"

    ok = error is None and last_finish == "stop"
    return {
        "ok": ok,
        "final_text": final_text,
        "turns": turns,
        "tool_calls": trace_calls,
        "allowed_tools": allowed_tool_names,
        "finish_reason": last_finish,
        "error": error,
    }
