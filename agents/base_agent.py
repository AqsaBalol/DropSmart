"""Base class for all DropSmart agents.

Every specialist agent (Supplier, Competitor, Fee, Margin, Risk, Report) and the
Orchestrator inherit from BaseAgent. This file is the single place where the Gemini
client is initialised, the API key is validated, and the shared logging contract is
defined — so none of that boilerplate repeats across seven agent files.
"""

# --- Standard library ---
import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any

# --- Third-party ---
import google.genai as genai


class BaseAgent(ABC):
    """Abstract base class shared by every DropSmart agent.

    Responsibilities handled here so subclasses do not repeat them:
    - Loading and validating the ``GEMINI_API_KEY`` from the environment.
    - Configuring the ``google.generativeai`` client once per agent instance.
    - Creating a named logger prefixed with ``dropsmart.<agent_name>`` so log
      output can be filtered per agent without code changes.
    - Exposing ``_log_start``, ``_log_end``, and ``_safe_generate`` helpers that
      every subclass can call without reimplementing error handling or logging.

    Subclasses must implement ``run(self, context: dict) -> dict``.

    Args:
        agent_name: Short identifier used in log messages and for diagnostics
            (e.g. ``"supplier_agent"``). Must be non-empty.
        model: Gemini model ID to use for generation. Defaults to
            ``"gemini-2.0-flash"`` which balances speed and quality for the
            structured research tasks in this pipeline.

    Raises:
        ValueError: If ``agent_name`` is empty or if ``GEMINI_API_KEY`` is not
            set in the environment.
    """

    def __init__(self, agent_name: str, model: str = "gemini-2.5-flash-lite") -> None:
        # Reject blank names immediately — a nameless agent produces unreadable logs
        if not agent_name or not agent_name.strip():
            raise ValueError("agent_name must be a non-empty string.")

        self._agent_name: str = agent_name.strip()
        self._model_id: str = model

        # --- Validate API key before anything else ---
        # Fail here rather than letting a missing key surface mid-pipeline as a
        # cryptic 403, which would leave the session context partially populated.
        api_key: str = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError(
                f"[{self._agent_name}] GEMINI_API_KEY is not set. "
                "Add it to your .env file before running DropSmart."
            )

        # --- Configure the shared google.generativeai client ---
        # configure() is a module-level call; calling it per-instance is safe
        # because it simply overwrites the module-level API key each time.

        # google.genai (new SDK) uses Client, not module-level configure()
        self._client = genai.Client(api_key=api_key)

        # --- Structured logger namespaced to this agent ---
        # Using a hierarchical name means a log filter of "dropsmart" captures
        # all agents, while "dropsmart.fee_agent" isolates just the fee agent.
        self._logger: logging.Logger = logging.getLogger(
            f"dropsmart.{self._agent_name}"
        )

    # ------------------------------------------------------------------
    # Abstract interface — every subclass must implement this
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """Executes the agent's primary task and returns its findings.

        The Orchestrator calls this method in sequence for each specialist
        agent. It receives the cumulative session context (all outputs from
        agents that ran before this one) and must return a dict that the
        Orchestrator merges into that context before calling the next agent.

        Returning a dict (rather than mutating ``context`` in place) keeps each
        agent independently testable — a test can pass a minimal context dict
        and inspect only the returned keys.

        Args:
            context: Cumulative session context assembled by the Orchestrator.
                Guaranteed to contain at minimum ``product``, ``marketplace``,
                ``business_model``, ``region``, ``packaging_cost``, and
                ``courier_cost``. Later agents receive additional keys added by
                earlier agents.

        Returns:
            A dict of this agent's findings. Keys are agent-specific (e.g. the
            Supplier Agent returns ``supplier_results``; the Fee Agent returns
            ``fee_structure``). The Orchestrator merges the returned dict into
            the session context with ``context.update(result)``.

        Raises:
            NotImplementedError: If a subclass forgets to implement this method
                (enforced automatically by the ABC machinery).
        """
        ...  # Concrete implementation required in every subclass

    # ------------------------------------------------------------------
    # Logging helpers
    # ------------------------------------------------------------------

    def _log_start(self, task: str) -> None:
        """Logs the beginning of a named task at INFO level.

        Using a dedicated start/end pair instead of ad-hoc log calls lets the
        Orchestrator's log stream show clearly where each agent begins work,
        which is valuable when debugging a multi-step pipeline failure.

        Args:
            task: Short description of the work being started
                (e.g. ``"searching supplier prices"``).
        """
        self._logger.info("[%s] START — %s", self._agent_name, task)

    def _log_end(self, task: str, success: bool) -> None:
        """Logs the completion of a named task.

        Logs at INFO on success and at WARNING on failure so that a failure
        stands out visually in the log stream without raising an exception —
        the caller decides whether to raise after calling this method.

        Args:
            task: The same description string passed to the matching
                ``_log_start`` call, so start/end pairs are easy to match.
            success: ``True`` if the task completed without error; ``False``
                if it completed with a recoverable error or partial result.
        """
        if success:
            self._logger.info("[%s] END (success) — %s", self._agent_name, task)
        else:
            # WARNING rather than ERROR because _log_end is called after the
            # agent has already handled the problem; ERROR is reserved for
            # unhandled exceptions that propagate out of _safe_generate.
            self._logger.warning(
                "[%s] END (failed) — %s", self._agent_name, task
            )

    # ------------------------------------------------------------------
    # Gemini generation helper
    # ------------------------------------------------------------------

    def _safe_generate(self, prompt: str) -> str:
        """Sends a prompt to Gemini using a model-fallback chain.

        Tries each model in the chain returned by ``_build_fallback_chain``
        in order:

        1. ``self._model_id`` — the agent's assigned model.
        2. ``"gemini-2.5-flash"`` — mid-tier fallback.
        3. ``"gemini-2.5-pro"``  — most capable fallback.

        Duplicates are removed so an agent already assigned one of the
        fallback models never issues two calls to the same endpoint.

        For each model the method attempts the call exactly once:

        - **Success**: returns ``response.text`` immediately. Logs at INFO
          if the call succeeded on a fallback model (not the first in chain).
        - **Transient error** (503 / UNAVAILABLE / RESOURCE_EXHAUSTED):
          logs a WARNING, waits 2 seconds, then moves to the next model.
        - **Non-transient error**: logs at ERROR and re-raises immediately
          without trying any further models.

        If every model in the chain fails with transient errors, logs at
        ERROR listing all attempted models and re-raises the last exception.

        Args:
            prompt: The full prompt string to send to Gemini. Must be
                non-empty. Callers are responsible for constructing
                well-formed prompts — this method sends them as-is.

        Returns:
            The response text from Gemini as a plain string.

        Raises:
            ValueError: If ``prompt`` is empty.
            Exception: Re-raises the SDK exception after logging it at
                ERROR level so the stack trace is preserved for the caller.
        """
        # Guard against accidentally sending an empty prompt — Gemini would
        # return an error anyway, but catching it here gives a cleaner message.
        if not prompt or not prompt.strip():
            raise ValueError(
                f"[{self._agent_name}] prompt must be a non-empty string."
            )

        chain: list[str] = self._build_fallback_chain()
        last_exc: Exception | None = None

        for attempt_idx, model_id in enumerate(chain):
            try:
                response = self._client.models.generate_content(
                    model=model_id, contents=prompt
                )
                # Log a fallback success so the operator can see which model
                # actually served the request during degraded conditions.
                if attempt_idx > 0:
                    self._logger.info(
                        "[%s] Gemini call succeeded on fallback model '%s' "
                        "(attempt %d/%d).",
                        self._agent_name,
                        model_id,
                        attempt_idx + 1,
                        len(chain),
                    )
                return response.text

            except Exception as exc:
                if self._is_transient_error(exc):
                    last_exc = exc
                    self._logger.warning(
                        "[%s] Transient error on model '%s': %s — "
                        "waiting 2 s before trying next model.",
                        self._agent_name,
                        model_id,
                        exc,
                    )
                    if attempt_idx < len(chain) - 1:
                        time.sleep(2)
                else:
                    # Non-transient: no point trying other models
                    self._logger.error(
                        "[%s] Non-transient Gemini error on model '%s': %s",
                        self._agent_name,
                        model_id,
                        exc,
                    )
                    raise

        # Every model in the chain exhausted — surface the final failure
        self._logger.error(
            "[%s] All %d model(s) in the fallback chain failed with "
            "transient errors: %s",
            self._agent_name,
            len(chain),
            chain,
        )
        raise last_exc  # type: ignore[misc]  # guaranteed non-None here

    def _build_fallback_chain(self) -> list[str]:
        """Returns the ordered list of model IDs to attempt, without duplicates.

        Always starts with ``self._model_id`` (the agent's assigned model),
        then appends ``"gemini-2.5-flash"`` and ``"gemini-2.5-pro"`` as
        fallbacks. Entries already present are skipped so the same model is
        never tried twice.

        Returns:
            Ordered list of model ID strings, length 1–3 depending on
            how many of the fallback models differ from ``self._model_id``.
        """
        _FALLBACKS: list[str] = ["gemini-2.5-flash", "gemini-2.5-pro"]
        seen: set[str] = set()
        chain: list[str] = []
        for model_id in [self._model_id, *_FALLBACKS]:
            if model_id not in seen:
                seen.add(model_id)
                chain.append(model_id)
        return chain

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        """Returns ``True`` for errors that warrant trying a fallback model.

        Checks the string representation of the exception for known transient
        signal strings. Transient errors indicate temporary service-side
        problems (overload, rolling restart) where a different model endpoint
        may succeed immediately.

        Recognised signals (case-insensitive):
        - ``"503"`` — HTTP Service Unavailable
        - ``"UNAVAILABLE"`` — gRPC status UNAVAILABLE
        - ``"RESOURCE_EXHAUSTED"`` — quota or rate-limit exceeded

        Args:
            exc: The exception raised by the Gemini SDK.

        Returns:
            ``True`` if the error is transient and a fallback attempt is
            warranted; ``False`` for auth errors, malformed requests, and
            any other non-recoverable failures.
        """
        msg: str = str(exc).upper()
        return any(
            signal in msg
            for signal in ("503", "UNAVAILABLE", "RESOURCE_EXHAUSTED")
        )
