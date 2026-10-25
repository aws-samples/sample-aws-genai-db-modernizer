#!/usr/bin/env python3
"""PreToolUse hook (Bash): refuse a call that prints a cloud credential helper's output (#1165).

A Bash call's output lands in the session log, so a helper that prints credentials
(``ada credentials print``, ``aws configure export-credentials``, ``aws sts
get-session-token`` and ``assume-role``, ``aws ecr get-login-password``, ``gcloud auth
print-access-token``, ``az account get-access-token``, ...) leaves them there. Blocked: such a
helper whose output is not

* captured by a variable assignment (``TOKEN=$(...)``, ``export TOKEN="$(...)"``, a backtick
  form), ``eval $(...)`` or ``source <(...)``,
* piped into a registry login that reads it from stdin (``docker login --password-stdin``,
  also ``podman``, ``helm registry``, ``buildah``, ``skopeo``), which prints nothing, nor
* sent somewhere that is not shown: a redirect (``>``, ``>>``, ``1>``, ``&>``) to a path other
  than ``/dev/stdout``, ``/dev/stderr``, ``/dev/tty`` and ``/dev/fd/*``, which print. A ``tee``
  of one of those paths prints too. A redirect to a real file or ``/dev/null`` is allowed
  because the output is not shown; keeping such a file private is up to the caller.

A pipe into anything else is not a way out (``... | jq .SecretAccessKey`` prints): end the
pipeline with such a redirect, or capture it. The program may be written with a path
(``/usr/local/bin/aws``) and a word may be quoted or escaped (``"aws" 'sts'``, a backslash
before a letter); those are normalised before matching. Only unquoted text is searched, so
``git commit -m "ada credentials print"`` is fine, while a ``$(...)`` or backtick inside double
quotes is still looked at. Stdin: the hook event JSON. Exit 0 allows, exit 2 blocks.

The match is linear in the command's length (a scan into simple commands, then a word-by-word
comparison, with no backtracking regex over repeated words), so a pathological command cannot
run into the hook's timeout, which would fail open.

Not a shell parser. Out of reach: a helper reached through ``bash -c "..."``, a variable or a
script, a word built from pieces (``"a"ws``), and a captured value printed afterwards
(``echo $TOKEN``). Reading ``~/.aws/credentials`` is a Read-permission matter
(``settings.ci.json`` denies it).
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

# (program, subcommand words in order): the helper prints credentials or a token to stdout.
_HELPERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ada", ("credentials", "print")),
    ("aws", ("configure", "export-credentials")),
    ("aws", ("sts", "get-session-token")),
    ("aws", ("sts", "assume-role")),
    ("aws", ("sts", "assume-role-with-saml")),
    ("aws", ("sts", "assume-role-with-web-identity")),
    ("aws", ("sts", "get-federation-token")),
    ("aws", ("ecr", "get-login-password")),
    ("aws", ("ecr", "get-authorization-token")),
    ("aws", ("codeartifact", "get-authorization-token")),
    ("aws", ("eks", "get-token")),
    ("gcloud", ("auth", "print-access-token")),
    ("gcloud", ("auth", "print-identity-token")),
    ("gcloud", ("auth", "application-default", "print-access-token")),
    ("az", ("account", "get-access-token")),
)
_PROGRAMS = {program for program, _ in _HELPERS}
_WRAPPERS = {"sudo", "env", "command", "time", "nice", "nohup", "exec", "builtin"}
_LOGIN_PROGRAMS = {"docker", "podman", "helm", "buildah", "skopeo"}
# The text before the helper says its output is captured, not printed. Only the last
# _LOOKBEHIND characters are searched, which keeps the match linear.
_LOOKBEHIND = 300
_ASSIGNED_TO_VARIABLE = re.compile(
    r"(?:^|[\s;&|(])(?:(?:export|local|declare(?:\s+-\S+)*|readonly|typeset)\s+)?"
    r"[A-Za-z_]\w*=\s*[\"']?(?:\$\(|`)\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:(?:sudo|env|command|time)\s+)*$"
)
_EVALUATED = re.compile(
    r"(?:\beval\s+[\"']?\$\(|(?:\bsource|\.)\s+<\()\s*(?:(?:sudo|env|command|time)\s+)*$"
)
# A redirect of stdout (not of stderr, and not `>&2`, which only moves it to stderr).
_STDOUT_REDIRECT = re.compile(r"(?:^|\s)(?:1|&)?>>?\s*([^\s&<>|;]\S*)")
_QUOTED_WORD = re.compile(r"([\"'])([\w./~+-]+)\1")
_ESCAPED_LETTER = re.compile(r"\\(?=[A-Za-z])")
_REASON = (
    "Blocked: `{what}` prints credentials, and a Bash call's output is kept in the session "
    "log. Capture it in a variable (TOKEN=$(...)), pass it to eval or source, or pipe it "
    "into a login that reads stdin (docker login --password-stdin); never print it."
)


def _prints(path: str) -> bool:
    """Whether a redirect target shows its content in the session log."""
    return path.startswith(("/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/fd/"))


def _normalised(command: str) -> str:
    """``command`` with escaped letters and quotes around one-word arguments removed, so
    a backslash-escaped or quoted ``aws "sts"`` reads as ``aws sts``."""
    return _QUOTED_WORD.sub(r"\2", _ESCAPED_LETTER.sub("", command))


def _visible(command: str) -> str:
    """``command`` with quoted text replaced by ``x``, keeping the ``$(...)`` and backtick
    substitutions inside double quotes, which still run."""
    out: list[str] = []
    quote: str | None = None
    depth = 0  # open "$(" inside double quotes
    tick = False  # inside a backtick substitution inside double quotes
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            out.append(ch if ch == "'" else "x")
            quote = None if ch == "'" else quote
        elif quote == '"' and depth == 0 and not tick:
            if ch == "\\" and i + 1 < len(command):
                out.append("xx")
                i += 1
            elif ch == '"':
                out.append(ch)
                quote = None
            elif command.startswith("$(", i):
                out.append("$(")
                depth = 1
                i += 1
            elif ch == "`":
                out.append(ch)
                tick = True
            else:
                out.append("x")
        elif quote == '"':  # inside a substitution that sits inside double quotes
            if tick:
                tick = ch != "`"
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            out.append(ch)
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "\\" and i + 1 < len(command):
            out.append("xx")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _is_redirection_ampersand(text: str, i: int) -> bool:
    return text[i - 1 : i] in (">", "<") or text[i + 1 : i + 2] == ">"


def _segments(text: str) -> list[tuple[int, str, str]]:
    """``(start, text, terminator)`` for each simple command; the terminator is the operator
    that ends it (``|``, ``;``, ``&&``, ``||``, ``&``, newline, ``)``, a backtick) or ``""``."""
    found: list[tuple[int, str, str]] = []
    start = 0
    i = 0
    while i < len(text):
        if text[i : i + 2] in ("&&", "||"):
            found.append((start, text[start:i], text[i : i + 2]))
            i += 2
            start = i
        elif text[i] in "|;&\n()`" and not (text[i] == "&" and _is_redirection_ampersand(text, i)):
            found.append((start, text[start:i], text[i]))
            i += 1
            start = i
        else:
            i += 1
    found.append((start, text[start:], ""))
    return found


def _helper_in(words: list[str]) -> str | None:
    """The helper a simple command runs, from its words, else None."""
    first = next((k for k, w in enumerate(words) if os.path.basename(w) in _PROGRAMS), None)
    if first is None:
        return None
    after_option = False
    for w in words[:first]:  # only wrappers, assignments and their options may come first
        if not (os.path.basename(w) in _WRAPPERS or "=" in w or w.startswith("-") or after_option):
            return None
        after_option = w.startswith("-")
    program = os.path.basename(words[first])
    rest = words[first + 1 :]
    for helper_program, sub in _HELPERS:
        if helper_program != program:
            continue
        at = 0
        for word in rest:
            if word == sub[at]:
                at += 1
                if at == len(sub):
                    return f"{program} {' '.join(sub)}"
    return None


def _is_login_sink(words: list[str]) -> bool:
    """``docker login --password-stdin ...``: reads the secret from stdin and prints none."""
    names = [os.path.basename(w) for w in words]
    return (
        bool(names)
        and names[0] in _LOGIN_PROGRAMS
        and "login" in names
        and "--password-stdin" in words
    )


def violation(command: str) -> str | None:
    """The helper whose output the command prints, or None when it is fine."""
    text = _visible(_normalised(command))
    segments = _segments(text)
    for index, (start, segment, _end) in enumerate(segments):
        what = _helper_in(segment.split())
        if what is None:
            continue
        before = text[max(0, start - _LOOKBEHIND) : start]
        if _ASSIGNED_TO_VARIABLE.search(before) or _EVALUATED.search(before):
            continue
        pipeline = [index]
        while segments[pipeline[-1]][2] == "|" and pipeline[-1] + 1 < len(segments):
            pipeline.append(pipeline[-1] + 1)
        if len(pipeline) > 1 and _is_login_sink(segments[pipeline[-1]][1].split()):
            continue
        prints = False
        redirected = False
        for k in pipeline:
            words = segments[k][1].split()
            if words and os.path.basename(words[0]) == "tee" and any(_prints(w) for w in words[1:]):
                prints = True
            for target in _STDOUT_REDIRECT.findall(segments[k][1]):
                if _prints(target):
                    prints = True
                else:
                    redirected = True
        if prints or not redirected:
            return what
    return None


def main() -> int:
    event = hook_common.read_event()
    if event.get("tool_name") not in (None, "Bash"):
        return 0
    command = hook_common.tool_input(event).get("command")
    if not isinstance(command, str):
        return 0
    what = violation(command)
    return int(hook_common.block(_REASON.format(what=what))) if what else 0


if __name__ == "__main__":
    sys.exit(main())
