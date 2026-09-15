"""Read a Jenkinsfile and hand back a pipeline draft.

The point of this module is the first hour of a port. A team arriving from
Jenkins has a declarative ``Jenkinsfile`` that already says what the stages are,
what each one runs, and what the job asks before it starts; retyping all of that
into the pipeline editor is the tax that stops the port happening at all. So it
is read here once and turned into the same payload the editor saves — stage
names, commands, and build parameters with their names and types — leaving the
person to fill in what a Jenkinsfile genuinely does not carry: stage images,
runner labels, secret values, registry settings.

**Nothing here writes.** :func:`parse` returns a draft. The editor applies it as
unsaved changes to be reviewed and saved by hand, because a translation that
silently replaced a working pipeline would be a worse tax than retyping.

What it is not
--------------
Not a Groovy interpreter, and not trying to be. ``script { }`` blocks, shared
libraries, and ``when`` clauses over anything but a plain comparison are
reported as notes rather than guessed at — a stage that runs *almost* the right
thing is the one failure mode worth avoiding, since it passes review and breaks
in production. Every note names the stage it came from, so the list doubles as
the to-do for finishing the port.

The reading strategy is a mask: every comment and every string literal is
blanked to spaces of the same length (see :func:`_mask`), so brace matching,
keyword lookup and statement splitting all run over text where a ``{`` inside a
shell heredoc cannot be mistaken for a block. Offsets stay valid against the
original, which is where literal values are read back from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# A Jenkinsfile is a configuration file. Anything past this is a Groovy program
# that will not translate anyway, and reading it would only take longer to say so.
MAX_SOURCE_CHARS = 512 * 1024

ERROR = "error"
WARNING = "warning"
INFO = "info"

# Mirrors of what pipelines.py accepts, kept here so a draft arrives already
# trimmed to a saveable size rather than being rejected on save.
MAX_PARAMETERS = 25
MAX_STAGES = 40
MAX_COMMANDS_PER_STAGE = 100
MAX_TEXT_CHARS = 4000
MAX_MULTILINE_CHARS = 64000
MAX_TIMEOUT_SECONDS = 24 * 3600
MIN_TIMEOUT_SECONDS = 30
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class JenkinsfileError(ValueError):
    """The source could not be read as a declarative pipeline. User-facing."""


# ---------------------------------------------------------------------------
# Masking and scanning
# ---------------------------------------------------------------------------

def _mask(text: str) -> str:
    """A same-length copy with comments and string literals blanked to spaces.

    Same length is the whole trick: every index found in the mask addresses the
    same character of the original, so structure is found in text that cannot
    contain a brace, and values are read back out of the real source.

    A literal keeps its quote characters and loses only its contents. The quotes
    are what makes ``sh 'x'`` distinguishable from ``sh`` with no argument at
    all — blank the whole span and every string argument in the file reads as
    an empty one.

    Interior newlines go too. A statement ends at a newline, and a forty-line
    ``sh '''...'''`` is one statement — keeping its newlines would split it into
    forty.
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        two = text[i : i + 2]
        three = text[i : i + 3]
        keep = 0
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif three in ("'''", '"""'):
            j = text.find(three, i + 3)
            j = n if j < 0 else j + 3
            keep = 3
        elif text[i] in ("'", '"'):
            quote = text[i]
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                # An unterminated single-line literal: stop at the line end
                # rather than blanking the rest of the file.
                if text[j] == "\n":
                    break
                j += 1
            keep = 1
        else:
            out.append(text[i])
            i += 1
            continue
        if keep and j - i >= keep * 2:
            out.append(text[i : i + keep])
            out.append(" " * (j - i - keep * 2))
            out.append(text[j - keep : j])
        else:
            out.append(" " * (j - i))
        i = j
    return "".join(out)


def _match(mask: str, start: int, opener: str, closer: str, limit: int) -> int:
    """Index of the bracket closing the one at ``start``, or -1."""
    depth = 0
    for i in range(start, min(len(mask), limit)):
        if mask[i] == opener:
            depth += 1
        elif mask[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


# A line ending in one of these is mid-expression, so the newline after it does
# not end the statement.
_CONTINUES = (",", "\\", "+", "&&", "||", "=", ":", "?", "|")


def _statement_end(mask: str, start: int, limit: int) -> int:
    depth = 0
    i = start
    while i < limit:
        ch = mask[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0:
            if ch in "{};":
                return i
            if ch == "\n" and not mask[start:i].rstrip().endswith(_CONTINUES):
                return i
        i += 1
    return limit


@dataclass
class _Stmt:
    """One statement — ``head(args) { body }``, with every part optional."""

    head: str
    args: Tuple[int, int]
    body: Tuple[int, int]
    span: Tuple[int, int]
    assigned: Tuple[int, int]  # value span of ``NAME = value``

    @property
    def has_args(self) -> bool:
        return self.args[1] > self.args[0]

    @property
    def has_body(self) -> bool:
        return self.body[1] > self.body[0]

    @property
    def has_assignment(self) -> bool:
        return self.assigned[1] > self.assigned[0]


_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")
_SPACE = " \t\r\n"


def _statements(mask: str, start: int, limit: int) -> List[_Stmt]:
    """Split a block body into its top-level statements."""
    out: List[_Stmt] = []
    i = start
    while i < limit:
        # A comma never starts a statement, and skipping it is what lets this
        # walker read a comma-separated list of calls — which is how the older
        # `properties([parameters([booleanParam(...), string(...)])])` form
        # declares build inputs.
        if mask[i] in " \t\r\n;," or mask[i] in "})]":
            i += 1
            continue
        match = _IDENT.match(mask, i)
        if not match or match.end() > limit:
            # Not a call — a bare literal, a closure, an operator line. Skip the
            # line rather than the character, so a stray token cannot make the
            # walker crawl.
            newline = mask.find("\n", i)
            i = limit if newline < 0 or newline >= limit else newline + 1
            continue

        head = mask[match.start() : match.end()]
        stmt_start = i
        cursor = match.end()
        args = (0, 0)
        assigned = (0, 0)

        probe = cursor
        while probe < limit and mask[probe] in " \t":
            probe += 1

        if probe < limit and mask[probe] == "(":
            close = _match(mask, probe, "(", ")", limit)
            close = limit - 1 if close < 0 else close
            args = (probe + 1, close)
            cursor = close + 1
        elif probe < limit and mask[probe] == "=" and mask[probe : probe + 2] != "==":
            stop = _statement_end(mask, probe + 1, limit)
            assigned = (probe + 1, stop)
            cursor = stop
        else:
            stop = _statement_end(mask, cursor, limit)
            if mask[cursor:stop].strip():
                args = (cursor, stop)
            cursor = stop

        body = (0, 0)
        probe = cursor
        while probe < limit and mask[probe] in " \t\r\n":
            probe += 1
        if probe < limit and mask[probe] == "{":
            close = _match(mask, probe, "{", "}", limit)
            close = limit - 1 if close < 0 else close
            body = (probe + 1, close)
            cursor = close + 1

        out.append(_Stmt(head, args, body, (stmt_start, cursor), assigned))
        i = max(cursor, stmt_start + 1)
    return out


def _named(statements: Sequence[_Stmt], name: str) -> Optional[_Stmt]:
    for stmt in statements:
        if stmt.head == name:
            return stmt
    return None


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

class Expr(str):
    """Groovy source we did not evaluate, carried so a caller can report it.

    Distinguishing this from ``str`` is what keeps ``defaultValue: params.FOO``
    from being written into a draft as the literal text ``params.FOO``.
    """


# Groovy's escapes in a single-line literal. Triple-quoted text is left exactly
# as written: it is almost always a shell script, where a backslash means what
# the shell says it means, and "fixing" it changes the program.
_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "$": "$",
}


def _unescape(raw: str) -> str:
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] == "\\" and i + 1 < n:
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
            continue
        out.append(raw[i])
        i += 1
    return "".join(out)


def _dedent_script(raw: str) -> str:
    """Strip the Groovy indentation off a triple-quoted shell script.

    ``sh '''`` starts with a newline and indents every line to match the
    surrounding Groovy. The indentation is not part of the script, and leaving
    it turns a ``cat <<EOF`` body into indented output.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\n"):
        text = text[1:]
    lines = text.split("\n")
    widths = [
        len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()
    ]
    if widths:
        common = min(widths)
        if common:
            lines = [line[common:] if line.strip() else line for line in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


@dataclass
class _Literal:
    """A parsed value plus how it was quoted, which decides interpolation."""

    value: Any
    gstring: bool = False  # written with " or """ — Groovy would interpolate


def _value(text: str, mask: str, start: int, end: int) -> _Literal:
    """Read one argument value out of ``text[start:end]``."""
    while start < end and mask[start] in " \t\r\n":
        start += 1
    while end > start and mask[end - 1] in " \t\r\n":
        end -= 1
    if start >= end:
        return _Literal("")

    raw = text[start:end]
    head3 = raw[:3]
    if head3 in ("'''", '"""') and raw.endswith(head3) and len(raw) >= 6:
        script = _dedent_script(raw[3:-3])
        if head3 == '"""':
            # Groovy would have eaten this backslash; the shell would not, and
            # `"\$PASSWORD"` reaching sh as a literal is the difference between
            # a docker login and a password printed into the build log.
            script = script.replace("\\$", "$")
        return _Literal(script, gstring=head3 == '"""')
    if raw[0] in ("'", '"') and len(raw) >= 2 and raw[-1] == raw[0]:
        return _Literal(_unescape(raw[1:-1]), gstring=raw[0] == '"')
    if raw in ("true", "false"):
        return _Literal(raw == "true")
    if raw == "null":
        return _Literal("")
    if re.fullmatch(r"-?\d+", raw):
        return _Literal(int(raw))
    if raw.startswith("["):
        close = _match(mask, start, "[", "]", end)
        if close > start:
            return _Literal(
                [item.value for item in _split_values(text, mask, start + 1, close)]
            )
    return _Literal(Expr(" ".join(raw.split())[:500]))


def _split_top_level(mask: str, start: int, end: int) -> List[Tuple[int, int]]:
    """Comma-separated spans of ``mask[start:end]``, ignoring nested brackets."""
    spans: List[Tuple[int, int]] = []
    depth = 0
    piece = start
    for i in range(start, end):
        ch = mask[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((piece, i))
            piece = i + 1
    spans.append((piece, end))
    return [span for span in spans if mask[span[0] : span[1]].strip()]


def _split_values(text: str, mask: str, start: int, end: int) -> List[_Literal]:
    return [_value(text, mask, a, b) for a, b in _split_top_level(mask, start, end)]


_NAMED_ARG = re.compile(r"\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*:")


def _arguments(
    text: str, mask: str, start: int, end: int
) -> Tuple[List[_Literal], Dict[str, _Literal]]:
    """Positional and named arguments of one call."""
    positional: List[_Literal] = []
    named: Dict[str, _Literal] = {}
    for a, b in _split_top_level(mask, start, end):
        match = _NAMED_ARG.match(mask, a, b)
        if match:
            named[match.group(1)] = _value(text, mask, match.end(), b)
        else:
            positional.append(_value(text, mask, a, b))
    return positional, named


def _stmt_args(text: str, mask: str, stmt: _Stmt):
    return _arguments(text, mask, stmt.args[0], stmt.args[1])


def _literal_from_source(source: str) -> _Literal:
    """Read a value out of a fragment lifted from a larger expression."""
    fragment = source.strip()
    return _value(fragment, _mask(fragment), 0, len(fragment))


def _str(literal: Optional[_Literal], limit: int = 512) -> str:
    """A literal as plain text, or "" when it was an expression we cannot read."""
    if literal is None or isinstance(literal.value, Expr):
        return ""
    if isinstance(literal.value, bool):
        return "true" if literal.value else "false"
    return str(literal.value)[:limit]


# ---------------------------------------------------------------------------
# Jenkins variables to KubeSight variables
# ---------------------------------------------------------------------------

# Every stage receives the build's variables as environment, so a Jenkins
# reference becomes a plain shell reference. The KUBESIGHT_* names are what
# engine.py exports on every stage.
_BUILT_INS = {
    "BUILD_NUMBER": "KUBESIGHT_BUILD_NUMBER",
    "BUILD_ID": "KUBESIGHT_BUILD_ID",
    "JOB_NAME": "KUBESIGHT_SERVICE",
    "JOB_BASE_NAME": "KUBESIGHT_SERVICE",
    "WORKSPACE": "KUBESIGHT_SOURCE",
    "BRANCH_NAME": "KUBESIGHT_BRANCH",
    "GIT_BRANCH": "KUBESIGHT_BRANCH",
    "GIT_COMMIT": "KUBESIGHT_COMMIT",
    "TAG_NAME": "KUBESIGHT_TAG",
}

# ${params.X}, ${env.X}, $params.X — the namespace is Groovy's, not the shell's.
_NAMESPACED = re.compile(r"\$\{\s*(?:params|env)\.([A-Za-z_][A-Za-z0-9_]*)[^}]*\}")
_BARE_NAMESPACED = re.compile(r"\$(?:params|env)\.([A-Za-z_][A-Za-z0-9_]*)")
_BRACED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_BARE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


def rewrite_variables(script: str) -> Tuple[str, List[str]]:
    """Turn Jenkins variable references into ones a build stage actually has.

    Returns the rewritten script and the built-in names that were renamed, so
    the caller can say what it changed instead of leaving a silent difference
    between the file and the draft.
    """
    renamed: List[str] = []

    def builtin(match: "re.Match[str]") -> str:
        name = match.group(1)
        target = _BUILT_INS.get(name)
        if not target:
            return match.group(0)
        if name not in renamed:
            renamed.append(name)
        return "${%s}" % target

    text = _NAMESPACED.sub(lambda m: "${%s}" % m.group(1), script)
    text = _BARE_NAMESPACED.sub(lambda m: "${%s}" % m.group(1), text)
    text = _BRACED.sub(builtin, text)
    text = _BARE.sub(builtin, text)
    return text, renamed


# ---------------------------------------------------------------------------
# The draft under construction
# ---------------------------------------------------------------------------

@dataclass
class _Draft:
    text: str
    mask: str
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    stages: List[Dict[str, Any]] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    secrets: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: List[Dict[str, str]] = field(default_factory=list)

    def note(self, level: str, message: str, *, stage: str = "", offset: int = -1) -> None:
        entry: Dict[str, Any] = {"level": level, "stage": stage, "message": message}
        if offset >= 0:
            entry["line"] = self.text.count("\n", 0, offset) + 1
        # Each distinct message is worth saying once; a pipeline with twelve
        # `script` blocks should produce one note about Groovy, not twelve.
        for existing in self.notes:
            if existing["message"] == message and existing["stage"] == stage:
                return
        self.notes.append(entry)

    def want_secret(self, name: str, *, credential_id: str, stage: str) -> None:
        entry = self.secrets.setdefault(
            name, {"name": name, "credentialId": credential_id, "usedBy": []}
        )
        if stage and stage not in entry["usedBy"]:
            entry["usedBy"].append(stage)
        if credential_id and not entry.get("credentialId"):
            entry["credentialId"] = credential_id


# ---------------------------------------------------------------------------
# parameters { }
# ---------------------------------------------------------------------------

# Jenkins parameter step -> KubeSight parameter type. The three that do not
# appear here are deliberate: `password` is a secret and gets a note, and
# anything unrecognised becomes text with a note, because a parameter that
# quietly vanishes is a build configured differently from the one intended.
_PARAM_KINDS = {
    "booleanParam": "boolean",
    "string": "text",
    "stringParam": "text",
    "text": "multiline",
    "textParam": "multiline",
    "choice": "choice",
    "choiceParam": "choice",
    "password": "text",
}

# git-parameter's types, which map onto the dialog's own ref picker.
_GIT_PARAM_SOURCES = {
    "PT_BRANCH": "branches",
    "PT_TAG": "tags",
    "PT_BRANCH_TAG": "branches_and_tags",
    "PT_REVISION": "branches",
}


def _read_parameters(draft: _Draft, start: int, end: int) -> None:
    for stmt in _statements(draft.mask, start, end):
        positional, named = _stmt_args(draft.text, draft.mask, stmt)
        name = _str(named.get("name"), 128)
        if not name and positional:
            name = _str(positional[0], 128)
        if not name:
            draft.note(
                WARNING,
                f"A {stmt.head}(...) parameter has no readable name and was skipped.",
                offset=stmt.span[0],
            )
            continue

        if len(draft.parameters) >= MAX_PARAMETERS:
            draft.note(
                WARNING,
                f"This job declares more than {MAX_PARAMETERS} parameters; "
                f"'{name}' and any after it were not imported.",
                offset=stmt.span[0],
            )
            break

        if not _PARAM_NAME_RE.match(name):
            draft.note(
                ERROR,
                f"Parameter '{name}' is not usable as an environment variable. "
                "Rename it (letters, digits and underscores) before saving, and "
                "update the commands that read it.",
                offset=stmt.span[0],
            )

        description = _str(named.get("description"), 500)
        param: Dict[str, Any] = {
            "name": name,
            "label": name,
            "description": description,
            "required": False,
        }

        if stmt.head == "gitParameter":
            param["type"] = "dynamic_choice"
            param["source"] = _GIT_PARAM_SOURCES.get(
                _str(named.get("type"), 32).upper(), "branches"
            )
            param["default"] = _str(named.get("defaultValue"), 255)
            draft.note(
                INFO,
                f"'{name}' came from git-parameter and now reads its options from "
                "the repository. The Run Build dialog already picks a branch or "
                "tag, so you may not need this parameter at all.",
                offset=stmt.span[0],
            )
            draft.parameters.append(param)
            continue

        kind = _PARAM_KINDS.get(stmt.head)
        if kind is None:
            kind = "text"
            draft.note(
                WARNING,
                f"'{stmt.head}' is not a parameter type KubeSight has; '{name}' was "
                "imported as a text input. Check its type before saving.",
                offset=stmt.span[0],
            )
        param["type"] = kind

        if stmt.head == "password":
            draft.note(
                WARNING,
                f"'{name}' was a password parameter. Build inputs are stored and "
                "shown in plain text — put the value in Secrets and reference it "
                "from the stage that needs it.",
                offset=stmt.span[0],
            )

        default = named.get("defaultValue")
        if default is None and len(positional) > 1:
            default = positional[1]

        if kind == "boolean":
            raw = default.value if default else False
            param["default"] = (
                "true"
                if (raw is True or (isinstance(raw, str) and raw.strip().lower() == "true"))
                else "false"
            )
        elif kind == "choice":
            choices = _choices(named.get("choices"))
            if not choices:
                draft.note(
                    WARNING,
                    f"Choice parameter '{name}' lists no readable options; it was "
                    "imported as a text input so the draft stays saveable.",
                    offset=stmt.span[0],
                )
                param["type"] = "text"
                param["default"] = ""
            else:
                param["choices"] = choices
                chosen = _str(default, 255)
                param["default"] = chosen if chosen in choices else choices[0]
        elif kind == "multiline":
            raw = default.value if default and not isinstance(default.value, Expr) else ""
            param["default"] = str(raw)[:MAX_MULTILINE_CHARS]
        else:
            if default is not None and isinstance(default.value, Expr):
                draft.note(
                    INFO,
                    f"'{name}' defaulted to a Groovy expression ({default.value}), "
                    "which cannot be evaluated here. Its default is empty.",
                    offset=stmt.span[0],
                )
            param["default"] = _str(default, MAX_TEXT_CHARS)

        draft.parameters.append(param)


_PROPERTIES_CALL = re.compile(r"(?<![\w.])properties\s*\(")
_PARAMETERS_CALL = re.compile(r"(?<![\w.])parameters\s*\(")


def _read_properties_parameters(draft: _Draft, start: int, end: int) -> bool:
    """The other place build inputs live: ``properties([parameters([...])])``.

    Jobs that predate declarative syntax — and any job that must also be
    launchable from the Jenkins UI — declare their inputs this way, as a call
    rather than a block. The parameter definitions inside are identical, so only
    the wrapper has to be unwrapped.

    Returns whether anything was found, so the caller knows not to look further.
    """
    found = False
    for outer in _PROPERTIES_CALL.finditer(draft.mask, start, end):
        open_paren = outer.end() - 1
        close = _match(draft.mask, open_paren, "(", ")", end)
        if close < 0:
            continue
        for inner in _PARAMETERS_CALL.finditer(draft.mask, open_paren, close):
            params_open = inner.end() - 1
            params_close = _match(draft.mask, params_open, "(", ")", close)
            if params_close < 0:
                continue
            # parameters([...]) — the list is what holds the definitions.
            bracket = draft.mask.find("[", params_open, params_close)
            if bracket < 0:
                continue
            bracket_close = _match(draft.mask, bracket, "[", "]", params_close)
            if bracket_close < 0:
                continue
            before = len(draft.parameters)
            _read_parameters(draft, bracket + 1, bracket_close)
            found = found or len(draft.parameters) > before
    return found


def _choices(literal: Optional[_Literal]) -> List[str]:
    """``choices`` is written as a list, or as one newline-separated string."""
    if literal is None:
        return []
    raw = literal.value
    items: List[Any]
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and not isinstance(raw, Expr):
        items = raw.splitlines()
    else:
        return []
    out: List[str] = []
    for item in items:
        if isinstance(item, Expr):
            continue
        text = str(item).strip()[:255]
        if text and text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# environment { }
# ---------------------------------------------------------------------------

_CREDENTIALS_CALL = re.compile(r"credentials\s*\(")


def _read_environment(
    draft: _Draft, start: int, end: int, *, stage: str = ""
) -> Tuple[Dict[str, str], List[Dict[str, str]]]:
    """``KEY = 'value'`` pairs, with ``credentials('id')`` split out as secrets."""
    env: Dict[str, str] = {}
    secret_refs: List[Dict[str, str]] = []
    for stmt in _statements(draft.mask, start, end):
        if not stmt.has_assignment:
            continue
        name = stmt.head
        if not _PARAM_NAME_RE.match(name):
            continue
        a, b = stmt.assigned
        match = _CREDENTIALS_CALL.search(draft.mask, a, b)
        if match:
            open_paren = match.end() - 1
            close = _match(draft.mask, open_paren, "(", ")", b)
            credential_id = ""
            if close > open_paren:
                values = _split_values(draft.text, draft.mask, open_paren + 1, close)
                credential_id = _str(values[0], 255) if values else ""
            draft.want_secret(name, credential_id=credential_id, stage=stage)
            secret_refs.append({"name": name, "envVar": name})
            # For a username/password credential Jenkins also defines NAME_USR
            # and NAME_PSW. A stage reading those would get empty strings here
            # and fail somewhere far from the cause, so it is said out loud.
            draft.note(
                INFO,
                f"{name} came from credentials('{credential_id}'). If any stage "
                f"reads {name}_USR or {name}_PSW, add those as two secrets — "
                "Jenkins split a username/password credential into them "
                "automatically and nothing does that here.",
                stage=stage,
            )
            continue
        literal = _value(draft.text, draft.mask, a, b)
        if isinstance(literal.value, Expr):
            draft.note(
                INFO,
                f"Environment variable {name} was set from a Groovy expression "
                f"({literal.value}); it was imported empty.",
                stage=stage,
                offset=a,
            )
            env[name] = ""
            continue
        value = _str(literal, MAX_TEXT_CHARS)
        if literal.gstring:
            value, _ = rewrite_variables(value)
        env[name] = value
    return env, secret_refs


# ---------------------------------------------------------------------------
# when { }
# ---------------------------------------------------------------------------

# `params.X == 'v'`, `env.X != "v"`, `X == 'v'`, and the bare `params.X`.
_COMPARISON = re.compile(
    r"^\s*(?:params|env)?\.?\s*\$?\{?\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}?\s*"
    r"(==|!=)\s*(.+?)\s*$"
)
_NEGATED_FLAG = re.compile(r"^\s*!\s*(?:params|env)?\.?([A-Za-z_][A-Za-z0-9_]*)\s*$")
_PLAIN_FLAG = re.compile(
    r"^\s*(?:params|env)?\.?([A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\.toBoolean\(\)|\s*==\s*true)?\s*$"
)


def _condition_from_expression(
    draft: _Draft, start: int, end: int, stage: str
) -> Optional[Dict[str, str]]:
    raw = draft.text[start:end].strip()
    if not raw:
        return None

    match = _COMPARISON.match(raw)
    if match:
        variable, operator, value = match.groups()
        literal = _literal_from_source(value)
        if isinstance(literal.value, Expr):
            return None
        return {
            "variable": variable,
            "operator": "equals" if operator == "==" else "not_equals",
            "value": _str(literal, 255),
        }

    match = _NEGATED_FLAG.match(raw)
    if match:
        return {"variable": match.group(1), "operator": "not_equals", "value": "true"}

    match = _PLAIN_FLAG.match(raw)
    if match:
        return {"variable": match.group(1), "operator": "equals", "value": "true"}

    return None


def _read_when(draft: _Draft, start: int, end: int, stage: str) -> Optional[Dict[str, str]]:
    """The one form a native run condition can hold: one variable, one value.

    A ``when`` with several clauses is reduced to the first that translates and
    the rest are reported. Inventing a compound evaluator would mean a condition
    language nobody asked for; naming what was dropped lets the author decide
    whether one gating input covers it.
    """
    conditions: List[Dict[str, str]] = []
    dropped: List[str] = []

    def walk(block_start: int, block_end: int, inside: str = "") -> None:
        for stmt in _statements(draft.mask, block_start, block_end):
            head = stmt.head
            if head in ("allOf", "anyOf", "not") and stmt.has_body:
                if head != "allOf":
                    dropped.append(head)
                walk(stmt.body[0], stmt.body[1], head)
                continue
            if head == "expression" and stmt.has_body:
                found = _condition_from_expression(
                    draft, stmt.body[0], stmt.body[1], stage
                )
                if found:
                    conditions.append(found)
                else:
                    dropped.append(
                        "expression { %s }"
                        % " ".join(draft.text[stmt.body[0] : stmt.body[1]].split())[:120]
                    )
                continue
            if head == "equals":
                _, named = _stmt_args(draft.text, draft.mask, stmt)
                actual = named.get("actual")
                variable = ""
                if actual is not None:
                    variable = re.sub(
                        r"^(?:params|env)\.", "", str(actual.value).strip()
                    )
                if _PARAM_NAME_RE.match(variable or ""):
                    conditions.append(
                        {
                            "variable": variable,
                            "operator": "equals",
                            "value": _str(named.get("expected"), 255),
                        }
                    )
                else:
                    dropped.append("equals")
                continue
            if head == "environment":
                _, named = _stmt_args(draft.text, draft.mask, stmt)
                variable = _str(named.get("name"), 128)
                if _PARAM_NAME_RE.match(variable or ""):
                    conditions.append(
                        {
                            "variable": variable,
                            "operator": "equals",
                            "value": _str(named.get("value"), 255),
                        }
                    )
                else:
                    dropped.append("environment")
                continue
            if head in ("branch", "tag"):
                positional, _ = _stmt_args(draft.text, draft.mask, stmt)
                pattern = _str(positional[0], 255) if positional else ""
                variable = "KUBESIGHT_BRANCH" if head == "branch" else "KUBESIGHT_TAG"
                if pattern and not any(ch in pattern for ch in "*?["):
                    conditions.append(
                        {"variable": variable, "operator": "equals", "value": pattern}
                    )
                else:
                    dropped.append(f"{head} '{pattern}'")
                continue
            if head in ("buildingTag", "changeRequest", "changeset", "triggeredBy", "beforeAgent",
                        "beforeInput", "beforeOptions", "anyOf", "allOf", "not", "expression"):
                dropped.append(head)
                continue
            dropped.append(head)

    walk(start, end)

    if dropped:
        draft.note(
            WARNING,
            "This stage's when-clause used " + ", ".join(sorted(set(dropped))[:4])
            + ", which a run condition cannot express. "
            + (
                "The remaining condition was kept."
                if conditions
                else "The stage was imported as always-run — gate it on a build input if it should not be."
            ),
            stage=stage,
        )
    if len(conditions) > 1:
        draft.note(
            WARNING,
            "This stage had several conditions; only "
            f"'{conditions[0]['variable']}' was kept. A run condition reads one "
            "variable — add a single build input that stands for the rest.",
            stage=stage,
        )
    return conditions[0] if conditions else None


# ---------------------------------------------------------------------------
# agent { }
# ---------------------------------------------------------------------------

def _read_agent(draft: _Draft, stmt: _Stmt, stage: str) -> Dict[str, Any]:
    """``label`` becomes runner labels, ``docker { image }`` becomes the image."""
    out: Dict[str, Any] = {}
    if not stmt.has_body:
        return out
    for inner in _statements(draft.mask, stmt.body[0], stmt.body[1]):
        positional, named = _stmt_args(draft.text, draft.mask, inner)
        if inner.head == "label":
            label = _str(positional[0], 64) if positional else ""
            if label:
                # `linux && docker` is a Jenkins label expression; each name is a
                # capability, which is exactly what runner labels are.
                out["runnerLabels"] = [
                    part.strip().lower()
                    for part in re.split(r"&&|\|\||,", label)
                    if part.strip()
                ]
        elif inner.head in ("docker", "kubernetes", "dockerfile"):
            if inner.head == "docker":
                image = _str(named.get("image"), 512)
                if not image and inner.has_body:
                    for deeper in _statements(draft.mask, inner.body[0], inner.body[1]):
                        if deeper.head == "image":
                            pos, _ = _stmt_args(draft.text, draft.mask, deeper)
                            image = _str(pos[0], 512) if pos else ""
                        elif deeper.head == "label":
                            pos, _ = _stmt_args(draft.text, draft.mask, deeper)
                            if pos:
                                out["runnerLabels"] = [_str(pos[0], 64).lower()]
                if image:
                    out["image"] = image
                if not image and not positional:
                    draft.note(
                        INFO,
                        "This stage used a docker agent whose image could not be "
                        "read; set the stage image by hand.",
                        stage=stage,
                    )
            elif inner.head == "dockerfile":
                draft.note(
                    WARNING,
                    "This stage built its own agent image from a Dockerfile. "
                    "Point the stage at a prebuilt image instead.",
                    stage=stage,
                )
            else:
                draft.note(
                    INFO,
                    "This stage declared a Kubernetes pod template. KubeSight's "
                    "Kubernetes runner builds the pod itself — set the stage image "
                    "and any runner labels instead.",
                    stage=stage,
                )
    return out


# ---------------------------------------------------------------------------
# steps { }
# ---------------------------------------------------------------------------

@dataclass
class _Steps:
    commands: List[str] = field(default_factory=list)
    artifacts: List[Dict[str, str]] = field(default_factory=list)
    secret_refs: List[Dict[str, str]] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    working_directory: str = ""
    checkout_only: bool = False
    has_checkout: bool = False
    timeout_seconds: int = 0
    renamed: List[str] = field(default_factory=list)


# Steps that produce no shell and need no note: they have no meaning outside
# Jenkins and dropping them changes nothing about what the stage does.
_IGNORED_STEPS = {
    "cleanWs",
    "deleteDir",
    "checkout",  # handled explicitly
    "milestone",
    "lock",
    "ansiColor",
    "timestamps",
    "wrap",
    "catchError",
    "warnError",
}

# Artifact-shaped steps: Jenkins pattern -> KubeSight artifact type.
_ARTIFACT_STEPS = {
    "archiveArtifacts": "binary",
    "junit": "test-report",
    "publishHTML": "test-report",
}


# Groovy, not steps. Their bodies still hold `sh` calls worth importing.
_GROOVY_CONTROL = {"if", "else", "for", "while", "switch", "try", "catch", "finally"}
# `def x = ...` and the typed forms of the same thing.
_GROOVY_DECLARATIONS = {"def", "var", "String", "int", "boolean", "Boolean", "List", "Map"}


def _assignment_target(draft: _Draft, stmt: _Stmt, head: str) -> str:
    """The name being assigned, for a note that can point at it."""
    if head in _GROOVY_DECLARATIONS:
        tail = draft.mask[stmt.span[0] : stmt.span[1]].split("=")[0].split()
        return tail[-1] if len(tail) > 1 else head
    return head


def _shell_from(literal: _Literal) -> str:
    script = str(literal.value)
    if isinstance(literal.value, Expr):
        return ""
    return script


def _read_steps(draft: _Draft, start: int, end: int, stage: str) -> _Steps:
    out = _Steps()
    statements = _statements(draft.mask, start, end)
    # A stage whose shell all lives in one dir('x') puts x on the stage instead
    # of in the script: that is what the field is for, and the commands then
    # read the way they did in Jenkins. Steps that produce no shell — archiving,
    # a cleanWs — do not count against it.
    shell_bearing = [
        stmt
        for stmt in statements
        if stmt.head not in _ARTIFACT_STEPS and stmt.head not in _IGNORED_STEPS
    ]
    only_dir = (
        len(shell_bearing) == 1
        and shell_bearing[0].head == "dir"
        and shell_bearing[0].has_body
    )

    def emit(lines: str) -> None:
        rewritten, renamed = rewrite_variables(lines)
        for name in renamed:
            if name not in out.renamed:
                out.renamed.append(name)
        for line in rewritten.split("\n"):
            out.commands.append(line.rstrip())

    def walk(block_start: int, block_end: int, indent: str = "") -> None:
        for stmt in _statements(draft.mask, block_start, block_end):
            head = stmt.head
            positional, named = _stmt_args(draft.text, draft.mask, stmt)

            if head in ("sh", "bat", "powershell", "pwsh"):
                literal = named.get("script") or (positional[0] if positional else None)
                if literal is None:
                    continue
                script = _shell_from(literal)
                if not script.strip():
                    continue
                if head != "sh":
                    draft.note(
                        ERROR,
                        f"This stage ran a {head} step. Builds run under /bin/sh on "
                        "Linux — the commands were imported verbatim and will not "
                        "run as written.",
                        stage=stage,
                        offset=stmt.span[0],
                    )
                if "returnStdout" in named or "returnStatus" in named:
                    draft.note(
                        WARNING,
                        "A step captured its output into a Groovy variable. Append "
                        'NAME=value to "$KUBESIGHT_ENV" instead — every later stage '
                        "sources it.",
                        stage=stage,
                        offset=stmt.span[0],
                    )
                emit("\n".join(indent + line if line.strip() else line
                               for line in script.split("\n")))
                continue

            if head == "echo":
                message = _str(positional[0], MAX_TEXT_CHARS) if positional else ""
                if message:
                    emit(indent + "echo " + _shell_quote(message))
                continue

            if head == "dir" and stmt.has_body:
                path = _str(positional[0], 512) if positional else ""
                if only_dir and not out.working_directory:
                    out.working_directory = path.strip("/")
                    walk(stmt.body[0], stmt.body[1], indent)
                    continue
                # A subshell, so the directory change cannot leak into the rest
                # of the script the way a bare `cd` would.
                out.commands.append(indent + "( cd " + _shell_quote(path))
                walk(stmt.body[0], stmt.body[1], indent + "  ")
                out.commands.append(indent + ")")
                continue

            if head == "withCredentials":
                _read_with_credentials(draft, stmt, stage, out)
                if stmt.has_body:
                    walk(stmt.body[0], stmt.body[1], indent)
                continue

            if head == "withEnv":
                for literal in (positional[0].value if positional else []) or []:
                    if isinstance(literal, (str, Expr)) and "=" in str(literal):
                        key, _, value = str(literal).partition("=")
                        if _PARAM_NAME_RE.match(key.strip()):
                            rewritten, _ = rewrite_variables(value)
                            out.env[key.strip()] = rewritten[:MAX_TEXT_CHARS]
                if stmt.has_body:
                    walk(stmt.body[0], stmt.body[1], indent)
                continue

            if head == "timeout" and stmt.has_body:
                seconds = _timeout_seconds(named)
                if seconds and not out.timeout_seconds:
                    out.timeout_seconds = seconds
                walk(stmt.body[0], stmt.body[1], indent)
                continue

            if head in ("retry", "waitUntil", "ws", "container", "node", "ansiColor",
                        "timestamps", "catchError", "warnError", "lock") and stmt.has_body:
                if head == "retry":
                    draft.note(
                        INFO,
                        "A retry() wrapper was flattened — its steps run once. Add "
                        "the retry to the command itself if it matters.",
                        stage=stage,
                        offset=stmt.span[0],
                    )
                walk(stmt.body[0], stmt.body[1], indent)
                continue

            if head == "script" and stmt.has_body:
                draft.note(
                    WARNING,
                    "This stage had a script { } block. Its Groovy was not "
                    "translated; any sh steps inside it were imported, and "
                    "anything else needs rewriting as shell.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                walk(stmt.body[0], stmt.body[1], indent)
                continue

            if head in _ARTIFACT_STEPS:
                pattern = _str(named.get("artifacts"), 512) or (
                    _str(positional[0], 512) if positional else ""
                )
                if pattern:
                    for part in pattern.split(","):
                        path = part.strip()
                        if path:
                            out.artifacts.append(
                                {"path": path, "type": _ARTIFACT_STEPS[head]}
                            )
                continue

            if head == "checkout":
                out.has_checkout = True
                continue

            if head in ("git",):
                out.has_checkout = True
                draft.note(
                    INFO,
                    "A git step was replaced by the checkout stage, which clones "
                    "the repository configured on this service.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                continue

            if head in ("stash", "unstash"):
                draft.note(
                    WARNING,
                    "stash/unstash has no equivalent: every stage of a build shares "
                    "one workspace, so files written by an earlier stage are already "
                    "there.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                continue

            if head == "input":
                draft.note(
                    WARNING,
                    "This stage paused for manual approval. A build cannot pause — "
                    "gate the stage on a build input, or split it into its own run.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                continue

            if head in ("emailext", "mail", "slackSend", "office365ConnectorSend"):
                draft.note(
                    INFO,
                    f"A {head} notification step was dropped. Build outcomes are "
                    "delivered by KubeSight's own alert routing.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                continue

            if head in _IGNORED_STEPS:
                continue

            if head in _GROOVY_CONTROL:
                draft.note(
                    WARNING,
                    f"A Groovy '{head}' ran here. Its branches were flattened — the "
                    "shell inside them was imported one after another, so rewrite "
                    "it as an if/fi in the commands.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                if stmt.has_body:
                    walk(stmt.body[0], stmt.body[1], indent)
                continue

            # `env.TAG = ...`, `def version = ...` — a Groovy binding, which is
            # the one Jenkins idiom with a direct replacement worth naming.
            if stmt.has_assignment or head in _GROOVY_DECLARATIONS:
                draft.note(
                    WARNING,
                    "This stage set a Groovy variable for later stages to read "
                    "(" + _assignment_target(draft, stmt, head) + "). Every stage "
                    'here is its own shell: append NAME=value to "$KUBESIGHT_ENV" '
                    "and the stages after it will have NAME in their environment.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                continue

            if stmt.has_body:
                draft.note(
                    INFO,
                    f"The '{head}' wrapper is not known here; the steps inside it "
                    "were imported without it.",
                    stage=stage,
                    offset=stmt.span[0],
                )
                walk(stmt.body[0], stmt.body[1], indent)
                continue

            draft.note(
                WARNING,
                f"Step '{head}' was not translated — it is a Jenkins plugin step "
                "with no shell equivalent. Replace it with a command.",
                stage=stage,
                offset=stmt.span[0],
            )

    walk(start, end)

    # Trailing blank lines from a dedented heredoc add nothing.
    while out.commands and not out.commands[-1].strip():
        out.commands.pop()
    if len(out.commands) > MAX_COMMANDS_PER_STAGE:
        draft.note(
            WARNING,
            f"This stage had more than {MAX_COMMANDS_PER_STAGE} lines of shell; the "
            "rest were dropped. Move the script into the repository and call it.",
            stage=stage,
        )
        del out.commands[MAX_COMMANDS_PER_STAGE:]

    out.checkout_only = out.has_checkout and not out.commands
    return out


def _shell_quote(value: str) -> str:
    if not value:
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`") + '"'


_TIME_UNITS = {
    "SECONDS": 1,
    "MINUTES": 60,
    "HOURS": 3600,
    "DAYS": 86400,
}


def _timeout_seconds(named: Dict[str, _Literal]) -> int:
    literal = named.get("time")
    if literal is None or isinstance(literal.value, Expr):
        return 0
    try:
        amount = int(literal.value)
    except (TypeError, ValueError):
        return 0
    unit = _TIME_UNITS.get(_str(named.get("unit"), 16).upper(), 60)
    seconds = amount * unit
    return max(MIN_TIMEOUT_SECONDS, min(seconds, MAX_TIMEOUT_SECONDS))


def _read_with_credentials(draft: _Draft, stmt: _Stmt, stage: str, out: _Steps) -> None:
    """Turn a credential binding into the secret names the stage will need.

    The secrets are *named*, never created: a value has to be typed in by hand
    under Secrets, and a stage that references one that does not exist is
    refused on save, which is the right moment to find out.
    """
    if not stmt.has_args:
        return
    bindings_start, bindings_end = stmt.args
    open_bracket = draft.mask.find("[", bindings_start, bindings_end)
    if open_bracket < 0:
        return
    close = _match(draft.mask, open_bracket, "[", "]", bindings_end)
    if close < 0:
        return
    for a, b in _split_top_level(draft.mask, open_bracket + 1, close):
        while a < b and draft.mask[a] in _SPACE:
            a += 1
        match = _IDENT.match(draft.mask, a, b)
        if not match:
            continue
        kind = draft.mask[match.start() : match.end()]
        paren = draft.mask.find("(", match.end(), b)
        if paren < 0:
            continue
        paren_close = _match(draft.mask, paren, "(", ")", b)
        if paren_close < 0:
            continue
        _, named = _arguments(draft.text, draft.mask, paren + 1, paren_close)
        credential_id = _str(named.get("credentialsId"), 255)
        targets = [
            _str(named.get(key), 128)
            for key in (
                "variable",
                "usernameVariable",
                "passwordVariable",
                "keyFileVariable",
                "tokenVariable",
                "secretVariable",
            )
        ]
        found = [name for name in targets if name and _PARAM_NAME_RE.match(name)]
        if not found:
            draft.note(
                WARNING,
                f"A {kind} credential binding did not name an environment variable "
                "and was skipped.",
                stage=stage,
                offset=a,
            )
            continue
        for name in found:
            draft.want_secret(name, credential_id=credential_id, stage=stage)
            if not any(ref["name"] == name for ref in out.secret_refs):
                out.secret_refs.append({"name": name, "envVar": name})
        if kind == "sshUserPrivateKey":
            draft.note(
                INFO,
                "An SSH key was bound to a file in Jenkins. Here the secret arrives "
                'as an environment variable — write it out with `printf %s "$KEY" > '
                '"$HOME/.ssh_key"` before use.',
                stage=stage,
                offset=a,
            )


# ---------------------------------------------------------------------------
# stages { }
# ---------------------------------------------------------------------------

def _unique_name(taken: set, name: str) -> str:
    """Stage names must be unique; parallel branches often are not."""
    candidate = name[:120] or "Stage"
    suffix = 2
    while candidate.lower() in taken:
        candidate = f"{name[:112]} ({suffix})"
        suffix += 1
    taken.add(candidate.lower())
    return candidate


def _read_stages(
    draft: _Draft,
    start: int,
    end: int,
    *,
    taken: set,
    parallel_group: str = "",
    inherited_labels: Optional[List[str]] = None,
) -> None:
    for stmt in _statements(draft.mask, start, end):
        if stmt.head != "stage" or not stmt.has_body:
            continue
        positional, _ = _stmt_args(draft.text, draft.mask, stmt)
        raw_name = _str(positional[0], 120) if positional else ""
        inner = _statements(draft.mask, stmt.body[0], stmt.body[1])

        parallel = _named(inner, "parallel")
        steps = _named(inner, "steps")

        if parallel is not None and parallel.has_body and steps is None:
            # The parent is a container, not a stage. Its branches become
            # consecutive stages tagged with its name, which is what the build
            # engine reads today and what a parallel executor would read later.
            group = raw_name or "parallel"
            draft.note(
                INFO,
                f"'{group}' ran its branches in parallel. They were imported as "
                "consecutive stages in the order they appear — a build runs its "
                "stages one after another.",
                stage=group,
                offset=stmt.span[0],
            )
            agent_stmt = _named(inner, "agent")
            labels = inherited_labels
            if agent_stmt is not None:
                labels = _read_agent(draft, agent_stmt, group).get("runnerLabels") or labels
            _read_stages(
                draft,
                parallel.body[0],
                parallel.body[1],
                taken=taken,
                parallel_group=group,
                inherited_labels=labels,
            )
            continue

        if len(draft.stages) >= MAX_STAGES:
            draft.note(
                WARNING,
                f"This job has more than {MAX_STAGES} stages; '{raw_name}' and any "
                "after it were not imported.",
                offset=stmt.span[0],
            )
            return

        name = _unique_name(taken, raw_name or f"Stage {len(draft.stages) + 1}")
        stage: Dict[str, Any] = {
            "name": name,
            "stageType": "command",
            "runnerType": "",
            "runnerLabels": list(inherited_labels or []),
            "image": "",
            "workingDirectory": "",
            "commands": [],
            "env": {},
            "secretRefs": [],
            "artifacts": [],
            "hostAliases": [],
            "runCondition": None,
            "timeoutSeconds": 1800,
            "continueOnFailure": False,
            "enabled": True,
        }
        if parallel_group:
            stage["parallelGroup"] = parallel_group

        agent_stmt = _named(inner, "agent")
        if agent_stmt is not None:
            stage.update(_read_agent(draft, agent_stmt, name))

        when_stmt = _named(inner, "when")
        if when_stmt is not None and when_stmt.has_body:
            stage["runCondition"] = _read_when(
                draft, when_stmt.body[0], when_stmt.body[1], name
            )

        env_stmt = _named(inner, "environment")
        if env_stmt is not None and env_stmt.has_body:
            env, refs = _read_environment(
                draft, env_stmt.body[0], env_stmt.body[1], stage=name
            )
            stage["env"].update(env)
            stage["secretRefs"].extend(refs)

        options_stmt = _named(inner, "options")
        if options_stmt is not None and options_stmt.has_body:
            for option in _statements(draft.mask, options_stmt.body[0], options_stmt.body[1]):
                if option.head == "timeout":
                    _, named = _stmt_args(draft.text, draft.mask, option)
                    seconds = _timeout_seconds(named)
                    if seconds:
                        stage["timeoutSeconds"] = seconds

        if steps is not None and steps.has_body:
            read = _read_steps(draft, steps.body[0], steps.body[1], name)
            stage["commands"] = read.commands
            stage["artifacts"] = read.artifacts
            stage["env"].update(read.env)
            for ref in read.secret_refs:
                if not any(existing["name"] == ref["name"] for existing in stage["secretRefs"]):
                    stage["secretRefs"].append(ref)
            if read.working_directory:
                stage["workingDirectory"] = read.working_directory
            if read.timeout_seconds:
                stage["timeoutSeconds"] = read.timeout_seconds
            if read.renamed:
                draft.note(
                    INFO,
                    "Jenkins variables were renamed to the ones a build exports: "
                    + ", ".join(
                        f"{name_} → {_BUILT_INS[name_]}" for name_ in read.renamed
                    )
                    + ".",
                    stage=name,
                )
            if read.checkout_only:
                stage["stageType"] = "checkout"
                stage["commands"] = []
                stage["timeoutSeconds"] = min(stage["timeoutSeconds"], 600)
            elif read.has_checkout:
                draft.note(
                    INFO,
                    "This stage checked out the repository and then ran commands. "
                    "The checkout is a stage of its own here — one was added ahead "
                    "of the pipeline.",
                    stage=name,
                )

        post_stmt = _named(inner, "post")
        if post_stmt is not None and post_stmt.has_body:
            draft.note(
                WARNING,
                "This stage had a post { } block (cleanup, or archiving on "
                "failure). It was not imported — a stage runs its commands and "
                "stops. Fold the cleanup into the commands, or add a stage that "
                "continues on failure.",
                stage=name,
                offset=post_stmt.span[0],
            )

        if not stage["commands"] and stage["stageType"] == "command":
            draft.note(
                ERROR,
                "No commands could be read for this stage, and a command stage "
                "needs at least one. Fill it in or remove the stage before saving.",
                stage=name,
                offset=stmt.span[0],
            )

        _suggest_container_image(draft, stage)
        draft.stages.append(stage)


_DOCKER_BUILD = re.compile(r"(?<![\w./-])docker(?:-compose)?\s+build\b")
_DOCKER_PUSH = re.compile(r"(?<![\w./-])docker\s+push\b")


def _suggest_container_image(draft: _Draft, stage: Dict[str, Any]) -> None:
    """Say when a stage is doing by hand what the image stage type does.

    Not applied automatically: the image name, the tag and the Dockerfile path
    all have to come off a command line this module would have to guess at, and
    a stage that builds the wrong image passes review.
    """
    script = "\n".join(stage.get("commands") or [])
    if not _DOCKER_BUILD.search(script):
        return
    draft.note(
        INFO,
        "This stage builds a container image by calling docker. Build pods have "
        "no Docker socket — switch it to a Build image stage, which builds and "
        "pushes without one"
        + (" (it pushes too, so the docker push lines go away)." if _DOCKER_PUSH.search(script) else "."),
        stage=stage["name"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _infer_missing_parameters(draft: _Draft) -> None:
    """Declare the inputs the stages gate on but nothing in the file defines.

    A Jenkins job can be parameterised in the job configuration rather than in
    the Jenkinsfile — the "This project is parameterized" checkboxes — and then
    the only trace in the file is the ``when`` clause that reads them. Importing
    those stages without their inputs produces a pipeline whose deploy stages
    can never fire and whose Run Build dialog asks nothing, which reads as the
    import having silently dropped something.

    So each gating variable with no definition gets one, typed from what the
    condition compares it to. Defaults are chosen so nothing fires by accident:
    a checkbox arrives unticked.
    """
    declared = {param["name"] for param in draft.parameters}
    for stage in draft.stages:
        condition = stage.get("runCondition") or {}
        name = condition.get("variable") or ""
        # KUBESIGHT_* are exported by the build itself, not asked for.
        if not name or name in declared or name.startswith("KUBESIGHT_"):
            continue
        if len(draft.parameters) >= MAX_PARAMETERS:
            break
        declared.add(name)
        value = (condition.get("value") or "").strip()
        if value.lower() in ("true", "false"):
            draft.parameters.append(
                {
                    "name": name,
                    "type": "boolean",
                    "label": name,
                    "description": f"Gates the '{stage['name']}' stage.",
                    "required": False,
                    "default": "false",
                }
            )
        else:
            draft.parameters.append(
                {
                    "name": name,
                    "type": "text",
                    "label": name,
                    "description": f"Gates the '{stage['name']}' stage (runs when this is \"{value}\").",
                    "required": False,
                    "default": "",
                }
            )
        draft.note(
            WARNING,
            f"'{name}' gates this stage but nothing in the Jenkinsfile declares "
            "it — it was a parameter on the Jenkins job itself. One was added so "
            "the stage can run; check its type and default.",
            stage=stage["name"],
        )


_LEVEL_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


def parse(content: str) -> Dict[str, Any]:
    """Read a declarative Jenkinsfile into a pipeline draft.

    Raises :class:`JenkinsfileError` only when there is nothing to read at all.
    Everything else is a note on the draft: a half-translated pipeline that says
    what it could not translate is useful, and refusing the file outright is not.
    """
    if not isinstance(content, str) or not content.strip():
        raise JenkinsfileError("Paste a Jenkinsfile, or upload one, to import it.")
    if len(content) > MAX_SOURCE_CHARS:
        raise JenkinsfileError(
            f"That file is larger than {MAX_SOURCE_CHARS // 1024} KB. A declarative "
            "Jenkinsfile is a configuration file — this looks like a program."
        )

    text = content.replace("\r\n", "\n").replace("\r", "\n")
    mask = _mask(text)
    draft = _Draft(text=text, mask=mask)

    top = _statements(mask, 0, len(mask))
    pipeline = _named(top, "pipeline")
    if pipeline is None or not pipeline.has_body:
        if "node" in {stmt.head for stmt in top} or "node {" in mask:
            raise JenkinsfileError(
                "This is a scripted pipeline (node { ... }), which is a Groovy "
                "program rather than a declaration. Only declarative pipelines "
                "(pipeline { ... }) can be imported."
            )
        raise JenkinsfileError(
            "No pipeline { ... } block was found. Import a declarative Jenkinsfile."
        )

    body = _statements(mask, pipeline.body[0], pipeline.body[1])

    params_stmt = _named(body, "parameters")
    if params_stmt is not None and params_stmt.has_body:
        _read_parameters(draft, params_stmt.body[0], params_stmt.body[1])
    else:
        # No declarative block. The inputs may still be declared as a call —
        # anywhere in the file, since `properties(...)` is as often written
        # above `pipeline {` as inside it.
        _read_properties_parameters(draft, 0, len(mask))

    env_stmt = _named(body, "environment")
    global_secret_refs: List[Dict[str, str]] = []
    if env_stmt is not None and env_stmt.has_body:
        draft.environment, global_secret_refs = _read_environment(
            draft, env_stmt.body[0], env_stmt.body[1]
        )

    default_labels: List[str] = []
    agent_stmt = _named(body, "agent")
    if agent_stmt is not None:
        default_labels = _read_agent(draft, agent_stmt, "").get("runnerLabels") or []

    default_timeout = 0
    options_stmt = _named(body, "options")
    if options_stmt is not None and options_stmt.has_body:
        for option in _statements(mask, options_stmt.body[0], options_stmt.body[1]):
            if option.head == "timeout":
                _, named = _stmt_args(text, mask, option)
                default_timeout = _timeout_seconds(named)

    stages_stmt = _named(body, "stages")
    if stages_stmt is None or not stages_stmt.has_body:
        raise JenkinsfileError(
            "The pipeline has no stages { ... } block, so there is nothing to import."
        )

    _read_stages(
        draft,
        stages_stmt.body[0],
        stages_stmt.body[1],
        taken=set(),
        inherited_labels=default_labels,
    )

    if _named(body, "post") is not None:
        draft.note(
            WARNING,
            "The job's post { } block was not imported. Build outcome handling "
            "belongs to KubeSight — notifications come from alert routing, and "
            "artifacts are collected by the stages that declare them.",
        )
    for section in ("triggers", "tools", "libraries"):
        if _named(body, section) is not None:
            draft.note(
                INFO,
                f"The '{section}' section was not imported."
                + (
                    " Schedule builds from the service's automation instead."
                    if section == "triggers"
                    else " Use a stage image that already carries the tooling."
                ),
            )

    # A checkout has to happen before anything reads the source, and Jenkins put
    # it in whichever stage called `checkout scm`.
    if draft.stages and not any(
        stage["stageType"] == "checkout" for stage in draft.stages
    ):
        draft.stages.insert(
            0,
            {
                "name": "Checkout",
                "stageType": "checkout",
                "runnerType": "",
                "runnerLabels": list(default_labels),
                "image": "",
                "workingDirectory": "",
                "commands": [],
                "env": {},
                "secretRefs": [],
                "artifacts": [],
                "hostAliases": [],
                "runCondition": None,
                "timeoutSeconds": 600,
                "continueOnFailure": False,
                "enabled": True,
            },
        )
        draft.note(
            INFO,
            "A Checkout stage was added first. Jenkins cloned the repository for "
            "the whole job; here it is a stage, so it can be seen and timed.",
        )

    # The pipeline-level environment is not a concept a stage has, so it is
    # written onto each stage — the stage's own values win, as they did in Groovy.
    if draft.environment or global_secret_refs:
        for stage in draft.stages:
            if stage["stageType"] == "checkout":
                continue
            merged = dict(draft.environment)
            merged.update(stage["env"])
            stage["env"] = merged
            for ref in global_secret_refs:
                if not any(existing["name"] == ref["name"] for existing in stage["secretRefs"]):
                    stage["secretRefs"].append(ref)
        if draft.environment:
            draft.note(
                INFO,
                f"The job's environment block ({', '.join(sorted(draft.environment))}) "
                "was copied onto every stage — a stage carries its own environment here.",
            )

    if default_timeout:
        for stage in draft.stages:
            stage["timeoutSeconds"] = min(stage["timeoutSeconds"], default_timeout)
        draft.note(
            INFO,
            "The job's overall timeout became a per-stage timeout. A build has no "
            "single clock; each stage has its own.",
        )

    # Last, so it sees every condition the stages ended up with.
    _infer_missing_parameters(draft)

    if not draft.parameters:
        draft.note(
            INFO,
            "This job asks for nothing before a build. If it should, its inputs "
            "were configured on the Jenkins job rather than in the Jenkinsfile — "
            "add them under Build inputs.",
        )

    draft.notes.sort(key=lambda note: (_LEVEL_ORDER.get(note["level"], 3), note["stage"]))

    counts = {
        ERROR: sum(1 for note in draft.notes if note["level"] == ERROR),
        WARNING: sum(1 for note in draft.notes if note["level"] == WARNING),
        INFO: sum(1 for note in draft.notes if note["level"] == INFO),
    }
    return {
        "parameters": draft.parameters,
        "stages": draft.stages,
        "secrets": list(draft.secrets.values()),
        "notes": draft.notes,
        "counts": {
            "stages": len(draft.stages),
            "parameters": len(draft.parameters),
            "errors": counts[ERROR],
            "warnings": counts[WARNING],
            "info": counts[INFO],
        },
        "summary": _summary(draft, counts),
    }


def _summary(draft: _Draft, counts: Dict[str, int]) -> str:
    stages = len(draft.stages)
    params = len(draft.parameters)
    parts = [
        f"{stages} {'stage' if stages == 1 else 'stages'}",
        f"{params} build {'input' if params == 1 else 'inputs'}",
    ]
    text = "Read " + " and ".join(parts) + "."
    if counts[ERROR]:
        text += f" {counts[ERROR]} need{'s' if counts[ERROR] == 1 else ''} filling in before this can be saved."
    elif counts[WARNING]:
        text += f" {counts[WARNING]} thing{'s' if counts[WARNING] != 1 else ''} to check."
    return text
