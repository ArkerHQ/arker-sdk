"""E2B Code Interpreter-compatible sandboxes backed by Arker VMs.

Replace ``from e2b_code_interpreter import ...`` with
``from arker.e2b_code_interpreter import ...``. Code runs in a stateful Python,
JavaScript or TypeScript interpreter per context, so variables and imports
carry over between runs.
Output reaches the callbacks once a run finishes, not while it runs.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from . import e2b
from ._compat import CodeContext, CodeContexts, CodeExecution, CompatSandbox, missing_export, unsupported
from .e2b import *  # noqa: F403 - this package extends e2b's, as E2B's does
from .e2b import ERRORS, HOME, InvalidArgumentException

DEFAULT_TIMEOUT = 300
# E2B's language names, and the core's.
LANGUAGES = {
    "python": "python",
    "js": "javascript",
    "javascript": "javascript",
    "ts": "typescript",
    "typescript": "typescript",
}
# What Result carries, beyond text: the formats Jupyter displays.
FORMATS = ("html", "markdown", "svg", "png", "jpeg", "pdf", "latex", "json", "javascript")


def __getattr__(name: str) -> Any:
    return missing_export(ERRORS, __name__, name)


@dataclasses.dataclass
class OutputMessage:
    line: str
    timestamp: int
    error: bool = False

    def __str__(self) -> str:
        return self.line


@dataclasses.dataclass
class ExecutionError:
    name: str
    value: str
    traceback: str

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))


class Result:
    """A value the code displayed: the cell's last expression (``is_main_result``), or a chart."""

    def __init__(self, text: str | None = None, *, is_main_result: bool = False, **formats: Any) -> None:
        self.text = text
        for name in FORMATS:
            setattr(self, name, formats.get(name))
        self.data = None
        self.chart = None
        self.extra = None
        self.is_main_result = is_main_result

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def formats(self) -> Iterable[str]:
        return [name for name in ("text", *FORMATS) if getattr(self, name) is not None]

    def __str__(self) -> str:
        return self.text or ""

    def __repr__(self) -> str:
        return f"Result({self.text})" if self.text else f"Result(Formats: {', '.join(self.formats())})"

    def _repr_html_(self) -> str | None:
        return self.html

    def _repr_markdown_(self) -> str | None:
        return self.markdown

    def _repr_svg_(self) -> str | None:
        return self.svg

    def _repr_png_(self) -> str | None:
        return self.png

    def _repr_jpeg_(self) -> str | None:
        return self.jpeg

    def _repr_pdf_(self) -> str | None:
        return self.pdf

    def _repr_latex_(self) -> str | None:
        return self.latex

    def _repr_json_(self) -> dict | None:
        return self.json

    def _repr_javascript_(self) -> str | None:
        return self.javascript


@dataclasses.dataclass
class Logs:
    stdout: list[str] = dataclasses.field(default_factory=list)
    stderr: list[str] = dataclasses.field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self))


@dataclasses.dataclass
class Execution:
    results: list[Result] = dataclasses.field(default_factory=list)
    logs: Logs = dataclasses.field(default_factory=Logs)
    error: ExecutionError | None = None
    execution_count: int | None = None

    @property
    def text(self) -> str | None:
        return next((result.text for result in self.results if result.is_main_result), None)

    def to_json(self) -> str:
        return json.dumps(
            {
                "results": [{name: result[name] for name in result.formats()} for result in self.results],
                "logs": self.logs.to_json(),
                "error": self.error.to_json() if self.error else None,
            }
        )


class Context:
    def __init__(self, context_id: str, language: str, cwd: str, **_: Any) -> None:
        self.id = context_id
        self.language = language
        self.cwd = cwd

    def __repr__(self) -> str:
        return f"Context(id={self.id!r}, language={self.language!r}, cwd={self.cwd!r})"


def _context(context: CodeContext) -> Context:
    return Context(context.id, context.language, context.cwd)


def _context_id(context: Context | str) -> str:
    return context if isinstance(context, str) else context.id


def _language(language: str | None) -> str:
    if language is None:
        return "python"
    if language not in LANGUAGES:
        raise unsupported(ERRORS, f"run_code(language={language!r})", "run other languages with commands.run.")
    return LANGUAGES[language]


def _execution(done: CodeExecution) -> Execution:
    return Execution(
        results=[Result(**result.data, is_main_result=result.main) for result in done.results],
        logs=Logs([done.stdout] if done.stdout else [], [done.stderr] if done.stderr else []),
        error=ExecutionError(done.error.name, done.error.value, done.error.traceback) if done.error else None,
        execution_count=done.count,
    )


class Sandbox(e2b.Sandbox):
    """An E2B code interpreter sandbox running as an Arker VM."""

    def _bind(self, sandbox: CompatSandbox) -> None:
        super()._bind(sandbox)
        self._contexts = CodeContexts(sandbox)

    def run_code(
        self,
        code: str,
        language: str | None = None,
        context: Context | None = None,
        on_stdout: Callable[[OutputMessage], Any] | None = None,
        on_stderr: Callable[[OutputMessage], Any] | None = None,
        on_result: Callable[[Result], Any] | None = None,
        on_error: Callable[[ExecutionError], Any] | None = None,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> Execution:
        """Run ``code`` in Python, or in JavaScript or TypeScript (``"js"``, ``"ts"``),
        on Node; an exception it raises is ``Execution.error``, not raised here."""
        if language and context:
            raise InvalidArgumentException("You can provide context or language, but not both at the same time.")
        timeout = None if timeout == 0 else (timeout or DEFAULT_TIMEOUT)
        done = self._contexts.run(
            code, context and context.id, cwd=HOME, envs=envs, timeout=timeout, language=_language(language)
        )
        execution = _execution(done)
        now = time.time_ns()
        if on_stdout:
            for line in execution.logs.stdout:
                on_stdout(OutputMessage(line, now, False))
        if on_stderr:
            for line in execution.logs.stderr:
                on_stderr(OutputMessage(line, now, True))
        if on_result:
            for result in execution.results:
                on_result(result)
        if on_error and execution.error:
            on_error(execution.error)
        return execution

    def create_code_context(
        self, cwd: str | None = None, language: str | None = None, request_timeout: float | None = None
    ) -> Context:
        return _context(self._contexts.create(cwd or HOME, language=_language(language)))

    def list_code_contexts(self) -> list[Context]:
        return [_context(context) for context in self._contexts.list()]

    def remove_code_context(self, context: Context | str) -> None:
        self._contexts.delete(_context_id(context))

    def restart_code_context(self, context: Context | str) -> None:
        self._contexts.restart(_context_id(context))


class AsyncSandbox(e2b.AsyncSandbox, wraps=Sandbox):
    """:class:`Sandbox` with coroutine methods, as ``e2b_code_interpreter.AsyncSandbox``;
    ``run_code`` callbacks may be coroutines."""

    _compat_path = "AsyncSandbox"
