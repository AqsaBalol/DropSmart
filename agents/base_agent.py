"""Base class for all DropSmart agents.

Every specialist agent (Supplier, Competitor, Fee, Margin, Risk, Report) and the
Orchestrator inherit from BaseAgent. This file is the single place where the Gemini
client is initialised, the API key is validated, and the shared logging contract is
defined — so none of that boilerplate repeats across seven agent files.
"""

# --- Standard library ---
import logging
import os
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

    def __init__(self, agent_name: str, model: str = "gemini-2.0-flash") -> None:
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
        genai.configure(api_key=api_key)

        # Create a model instance bound to this agent; subclasses call
        # _safe_generate() and never touch self._gemini_model directly.
        self._gemini_model = genai.GenerativeModel(model_name=self._model_id)

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
        """Sends a prompt to Gemini and returns the response text.

        Wraps the raw ``generate_content`` call so that every subclass gets
        consistent error logging and a guaranteed string return type without
        duplicating try/except blocks in each agent file.

        The method re-raises the exception after logging it. This is
        intentional — the calling agent's ``run()`` method should decide
        whether to retry, fall back to a default, or let the error propagate
        to the Orchestrator. Swallowing the exception here would hide failures
        that the pipeline needs to surface at the HITL checkpoint.

        Args:
            prompt: The full prompt string to send to Gemini. Must be
                non-empty. Callers are responsible for constructing
                well-formed prompts — this method sends them as-is.

        Returns:
            The response text from Gemini as a plain string.

        Raises:
            ValueError: If ``prompt`` is empty.
            Exception: Re-raises any exception from the Gemini SDK after
                logging it at ERROR level so the stack trace is preserved.
        """
        # Guard against accidentally sending an empty prompt — Gemini would
        # return an error anyway, but catching it here gives a cleaner message.
        if not prompt or not prompt.strip():
            raise ValueError(
                f"[{self._agent_name}] prompt must be a non-empty string."
            )

        try:
            response = self._gemini_model.generate_content(prompt)
            # Access .text directly — if generation was blocked by safety
            # filters, this raises an exception, which the except block catches.
            return response.text

        except Exception as exc:
            # Log at ERROR so this is always visible in the log stream, even
            # when the root logger's level is set to WARNING in production.
            self._logger.error(
                "[%s] Gemini generation failed: %s", self._agent_name, exc
            )
            # Re-raise so the Orchestrator can decide how to handle it
            raise
