#!/usr/bin/env python3
"""khms_prompt.py -- render a pipeline stage prompt: the shipped English template plus deployment fields.

Usage: python3 khms_prompt.py <name>          print prompts/<name>.md rendered (rc 3 when it cannot be)
       python3 khms_prompt.py --check <name>  render, then list the fields and slots it used

A template is plain Markdown with two kinds of placeholder:

  {{FIELD}}        replaced by a resolved value. Fields: KHMS_ROOT (the data root), KHMS_SPEC (the card
                   schema; default spec/khms-spec.md of this clone), PRINCIPAL (KHMS_PRINCIPAL: who the
                   base serves), RECALL_LOG, PRECHECK_LOG and INJECT_LOG (the agent's logs).
  {{EXT:<slot>}}   on a line of its own: replaced by the private extension file
                   ${KHMS_PROMPT_EXT}/<name>.<slot>.md (its text, verbatim, without the final newline).
                   A missing file removes the line.
  {{EXT:<slot>}}   the block form: the lines between the two markers are the SHIPPED default and are
  ...              replaced as a whole by the extension file when it exists. Extensions carry what is
  {{/EXT}}         specific to one deployment -- its project's sources, its principal's language, its
                   own conventions -- and never enter this repository.

An unknown field, a malformed placeholder or a missing template is an error (rc 3, one line on stderr):
a stage run with half-rendered instructions is worse than a stage that does not run.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import khms_paths as P  # noqa: E402

PROMPTS = os.path.join(P.CODE, "prompts")
FIELD_RE = re.compile(r"\{\{([A-Z_]+)\}\}")
BLOCK_RE = re.compile(r"^[ \t]*\{\{EXT:([a-z0-9-]+)\}\}[ \t]*\n(.*?)^[ \t]*\{\{/EXT\}\}[ \t]*\n?", re.M | re.S)
EXT_RE = re.compile(r"^[ \t]*\{\{EXT:([a-z0-9-]+)\}\}[ \t]*\n?", re.M)
ANY_RE = re.compile(r"\{\{[^}]*\}\}")


class PromptError(Exception):
    pass


def fields():
    spec = P.get("KHMS_SPEC") or os.path.join(P.REPO, "spec", "khms-spec.md")
    return {
        "KHMS_ROOT": P.get("KHMS_ROOT"),
        "KHMS_SPEC": spec,
        "PRINCIPAL": P.get("KHMS_PRINCIPAL"),
        "RECALL_LOG": P.get("KHMS_RECALL_LOG"),
        "INJECT_LOG": P.get("KHMS_INJECT_LOG"),
        "PRECHECK_LOG": P.get("KHMS_PRECHECK_LOG"),
    }


def render(name, used=None):
    path = os.path.join(PROMPTS, name + ".md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise PromptError(f"khms_prompt: no template {path} ({exc.strerror})")
    ext_dir = P.get("KHMS_PROMPT_EXT")

    def ext(m):
        slot = m.group(1)
        default = m.group(2) if m.re is BLOCK_RE else ""
        f = os.path.join(ext_dir, f"{name}.{slot}.md")
        if not os.path.isfile(f):
            if used is not None:
                used.append(f"EXT:{slot} (shipped default)")
            return default
        with open(f, encoding="utf-8") as fh:
            body = fh.read()
        if used is not None:
            used.append(f"EXT:{slot} <- {f}")
        if body.endswith("\n"):
            body = body[:-1]
        return body + "\n"

    text = BLOCK_RE.sub(ext, text)
    text = EXT_RE.sub(ext, text)
    vals = fields()

    def field(m):
        key = m.group(1)
        if key not in vals:
            raise PromptError(f"khms_prompt: {name}: unknown field {{{{{key}}}}}")
        if used is not None:
            used.append(key)
        return vals[key]

    text = FIELD_RE.sub(field, text)
    left = ANY_RE.search(text)
    if left:
        raise PromptError(f"khms_prompt: {name}: malformed placeholder {left.group(0)}")
    return text


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().split("\n\n")[0])
        return 0 if argv else 2
    check = argv[0] == "--check"
    name = argv[1] if check and len(argv) > 1 else argv[0]
    used = [] if check else None
    try:
        text = render(name, used)
    except (PromptError, P.KhmsPathError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if check:
        for u in used:
            print(u)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
