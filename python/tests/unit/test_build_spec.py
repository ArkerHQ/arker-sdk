"""Dockerfile parsing for the SDK-driven build.

The SDK builds a Dockerfile by forking from `FROM` and then driving each
instruction itself - `RUN` becomes `vm.run(...)`, `COPY` becomes a directory
sync into the VM. This module is the parse half: text in, an ordered list of
instructions out, with no network and no VM.

Parsing lives client-side because `COPY` is what makes a Dockerfile build
impossible to serve purely server-side - its sources are files on THIS machine,
and only the client can read them.
"""

from __future__ import annotations

import pytest

from arker.build_spec import (
    Add,
    Cmd,
    Copy,
    DockerfileError,
    Entrypoint,
    Env,
    Run,
    User,
    Workdir,
    parse_dockerfile,
)


def test_parses_a_plain_single_stage_dockerfile():
    parsed = parse_dockerfile("FROM ubuntu:24.04\nRUN apt-get update\nENV FOO=bar\nWORKDIR /app\nUSER root\n")
    assert parsed.base_image == "ubuntu:24.04"
    assert parsed.steps == [
        Run("apt-get update"),
        Env([("FOO", "bar")]),
        Workdir("/app"),
        User("root"),
    ]


def test_comments_and_blank_lines_are_ignored():
    parsed = parse_dockerfile("# leading comment\n\nFROM ubuntu:24.04\n   # indented comment\nRUN echo hi\n")
    assert parsed.base_image == "ubuntu:24.04"
    assert parsed.steps == [Run("echo hi")]


def test_line_continuations_join_into_one_instruction():
    """The continued line's indentation is preserved verbatim.

    We deliberately do NOT collapse the whitespace: it is insignificant to the
    shell here, but collapsing would corrupt a command where the spaces sit
    inside quotes (`RUN echo "a    b"`). One Run step is what matters.
    """
    parsed = parse_dockerfile("FROM ubuntu:24.04\nRUN apt-get update \\\n    && apt-get install -y curl\n")
    assert parsed.steps == [Run("apt-get update     && apt-get install -y curl")]


def test_whitespace_inside_a_quoted_run_argument_is_preserved():
    """The reason the test above does not normalise whitespace."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nRUN echo "a    b"\n')
    assert parsed.steps == [Run('echo "a    b"')]


def test_instruction_keywords_are_case_insensitive():
    parsed = parse_dockerfile("from ubuntu:24.04\nrun echo hi\n")
    assert parsed.base_image == "ubuntu:24.04"
    assert parsed.steps == [Run("echo hi")]


def test_copy_carries_sources_and_destination():
    parsed = parse_dockerfile("FROM ubuntu:24.04\nCOPY a.txt b.txt /srv/\n")
    assert parsed.steps == [Copy(["a.txt", "b.txt"], "/srv/")]


def test_copy_of_the_whole_context():
    """The single most common COPY line there is."""
    parsed = parse_dockerfile("FROM ubuntu:24.04\nCOPY . /app\n")
    assert parsed.steps == [Copy(["."], "/app")]


def test_copy_chown_is_carried_not_dropped():
    """Docker applies --chown to the written files; silently ignoring it would
    hand back a tree with the wrong owner and no indication why."""
    parsed = parse_dockerfile("FROM ubuntu:24.04\nCOPY --chown=app:app src /app\n")
    assert parsed.steps == [Copy(["src"], "/app", chown="app:app")]


def test_env_line_with_several_pairs_becomes_one_step():
    parsed = parse_dockerfile("FROM ubuntu:24.04\nENV A=1 B=2\n")
    assert parsed.steps == [Env([("A", "1"), ("B", "2")])]


def test_env_legacy_space_form():
    """`ENV key value` - Docker's older single-pair spelling, still common."""
    parsed = parse_dockerfile("FROM ubuntu:24.04\nENV FOO bar baz\n")
    assert parsed.steps == [Env([("FOO", "bar baz")])]


def test_env_quoted_value_keeps_inner_spaces():
    parsed = parse_dockerfile('FROM ubuntu:24.04\nENV GREETING="hello world"\n')
    assert parsed.steps == [Env([("GREETING", "hello world")])]


def test_run_exec_form_becomes_a_quoted_command_line():
    """Exec form has no shell in Docker; our only primitive is a command line,
    so each element is quoted and joined - same argv, via the shell's own
    fork+exec. `shlex.quote` quotes only what needs it, so `echo` stays bare."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nRUN ["echo", "a b"]\n')
    assert parsed.steps == [Run("echo 'a b'")]


def test_run_exec_form_quotes_shell_metacharacters():
    """The reason quoting happens at all: an argument that looks like shell
    syntax must reach the program as one literal argument."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nRUN ["echo", "a; rm -rf /"]\n')
    assert parsed.steps == [Run("echo 'a; rm -rf /'")]


def test_add_with_a_url_is_kept_as_a_fetch():
    parsed = parse_dockerfile("FROM ubuntu:24.04\nADD https://example.com/f /f\n")
    assert parsed.steps == [Add("https://example.com/f", "/f")]


def test_entrypoint_and_cmd_are_recorded():
    parsed = parse_dockerfile('FROM ubuntu:24.04\nENTRYPOINT ["/bin/sh"]\nCMD ["-c", "true"]\n')
    assert parsed.steps == [Entrypoint("/bin/sh"), Cmd("-c true")]


def test_rejects_a_dockerfile_with_no_from():
    with pytest.raises(DockerfileError, match="FROM"):
        parse_dockerfile("RUN echo hi\n")


def test_rejects_multi_stage_builds():
    with pytest.raises(DockerfileError, match="multi-stage"):
        parse_dockerfile("FROM ubuntu:24.04 AS build\nRUN echo hi\nFROM alpine:3.20\n")


def test_rejects_cross_stage_copy():
    with pytest.raises(DockerfileError, match="--from"):
        parse_dockerfile("FROM ubuntu:24.04\nCOPY --from=build /a /a\n")


def test_rejects_arg_substituted_from():
    """We resolve FROM through a real image fork, so it must be a literal."""
    with pytest.raises(DockerfileError, match="ARG"):
        parse_dockerfile("ARG BASE=ubuntu:24.04\nFROM ${BASE}\n")


def test_rejects_unsupported_directives_by_name():
    for directive in ["VOLUME /data", "HEALTHCHECK CMD true", "ONBUILD RUN x"]:
        with pytest.raises(DockerfileError, match=directive.split()[0]):
            parse_dockerfile(f"FROM ubuntu:24.04\n{directive}\n")


def test_rejects_copy_with_no_destination():
    with pytest.raises(DockerfileError, match="COPY"):
        parse_dockerfile("FROM ubuntu:24.04\nCOPY onlyone\n")


def test_rejects_empty_input():
    with pytest.raises(DockerfileError):
        parse_dockerfile("   \n\n")


def test_an_env_key_that_would_inject_a_command_is_refused():
    """A key is a bare token in `export k=v` and cannot be quoted, so it is the
    one interpolation quoting cannot make safe. Docker refuses the same input
    with "invalid environment variable name"."""
    # NB `ENV a b=c=1` is NOT in this list: that is the legacy `ENV key value`
    # form, so the key is `a` and the value is `b=c=1`. Valid, and accepted.
    for bad in ["ENV a;id;b=1", "ENV `id`=1", "ENV 1leading=x", "ENV a-b=x"]:
        with pytest.raises(DockerfileError, match="valid environment variable name"):
            parse_dockerfile(f"FROM ubuntu:24.04\n{bad}\n")


def test_an_arg_name_is_validated_too():
    with pytest.raises(DockerfileError, match="valid environment variable name"):
        parse_dockerfile("FROM ubuntu:24.04\nARG x;touch /tmp/pwn;y=2\n")


def test_ordinary_env_and_arg_names_still_parse():
    parsed = parse_dockerfile("FROM ubuntu:24.04\nENV _A1=x B=y\nARG VERSION=1\n")
    assert parsed.steps[0] == Env([("_A1", "x"), ("B", "y")])


def test_parsing_does_not_write_into_the_working_directory(tmp_path, monkeypatch):
    existing = tmp_path / "Dockerfile"
    existing.write_text("FROM myrealbase:1\nRUN important-thing\n")
    monkeypatch.chdir(tmp_path)

    parse_dockerfile("FROM ubuntu:24.04\nRUN echo hi\n")

    assert existing.read_text() == "FROM myrealbase:1\nRUN important-thing\n"
    assert sorted(q.name for q in tmp_path.iterdir()) == ["Dockerfile"]


# --- SHELL -----------------------------------------------------------------
#
# Docker's SHELL replaces the interpreter used for the SHELL FORM of RUN. Its
# common real-world use is `["/bin/bash", "-o", "pipefail", "-c"]`: plain `sh`
# reports only the last exit code in a pipeline, so `curl bad | tar xz`
# succeeds even when the download failed. Dropping SHELL silently is what makes
# that pipeline look green in a broken image.


def test_shell_sets_the_interpreter_for_a_later_run():
    parsed = parse_dockerfile(
        'FROM ubuntu:24.04\nSHELL ["/bin/bash", "-o", "pipefail", "-c"]\nRUN curl -f url | tar xz\n'
    )
    assert parsed.steps == [Run("/bin/bash -o pipefail -c 'curl -f url | tar xz'")]


def test_run_before_a_shell_keeps_the_default_interpreter():
    """SHELL applies forward only, so an earlier RUN is untouched."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nRUN echo early\nSHELL ["/bin/bash", "-c"]\nRUN echo late\n')
    assert parsed.steps == [Run("echo early"), Run("/bin/bash -c 'echo late'")]


def test_a_second_shell_replaces_the_first():
    parsed = parse_dockerfile('FROM ubuntu:24.04\nSHELL ["/bin/bash", "-c"]\nSHELL ["/bin/zsh", "-c"]\nRUN echo hi\n')
    assert parsed.steps == [Run("/bin/zsh -c 'echo hi'")]


def test_exec_form_run_ignores_shell():
    """Exec form does not go through an interpreter in Docker, so SHELL cannot apply."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nSHELL ["/bin/bash", "-c"]\nRUN ["echo", "hi"]\n')
    assert parsed.steps == [Run("echo hi")]


def test_shell_does_not_leak_into_env_or_workdir():
    """Only RUN takes the interpreter; ENV/WORKDIR stay plain instructions."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nSHELL ["/bin/bash", "-c"]\nENV A=b\nWORKDIR /app\n')
    assert parsed.steps == [Env([("A", "b")]), Workdir("/app")]


def test_shell_must_be_given_in_exec_form():
    """Docker requires the JSON-array form; the shell form is an error there too."""
    with pytest.raises(DockerfileError, match="exec form"):
        parse_dockerfile("FROM ubuntu:24.04\nSHELL /bin/bash -c\n")


def test_shell_must_not_be_empty():
    with pytest.raises(DockerfileError, match="at least one"):
        parse_dockerfile("FROM ubuntu:24.04\nSHELL []\n")


def test_shell_applies_to_shell_form_entrypoint_and_cmd():
    """Docker's SHELL rewrites the shell form of CMD and ENTRYPOINT too, not just RUN."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nSHELL ["/bin/bash", "-c"]\nENTRYPOINT echo hi\nCMD echo bye\n')
    assert parsed.steps == [Entrypoint("/bin/bash -c 'echo hi'"), Cmd("/bin/bash -c 'echo bye'")]


def test_exec_form_entrypoint_and_cmd_ignore_shell():
    parsed = parse_dockerfile(
        'FROM ubuntu:24.04\nSHELL ["/bin/bash", "-c"]\nENTRYPOINT ["echo", "hi"]\nCMD ["echo", "bye"]\n'
    )
    assert parsed.steps == [Entrypoint("echo hi"), Cmd("echo bye")]


def test_shell_quoting_of_an_embedded_single_quote():
    """Locks the exact bytes, so the TypeScript SDK can assert the same string."""
    parsed = parse_dockerfile('FROM ubuntu:24.04\nSHELL ["/bin/bash", "-c"]\nRUN echo \'hi\'\n')
    assert parsed.steps == [Run("/bin/bash -c 'echo '\"'\"'hi'\"'\"''")]
