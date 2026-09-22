"""`.env.example` is git-tracked (unlike `.env`, which is gitignored) --
it exists to document variable NAMES, never real values. A real secret
pasted into it once already (see LESSONS_LEARNED.md's Step 5 entry): an
uncommitted working-tree edit, caught before it was ever staged, but one
`git add -A` away from leaking into permanent git history. This test is
the guardrail that catches it next time, in the normal suite, before a
commit -- not a human re-reading the diff by hand.
"""
import os
import re
import unittest

ENV_EXAMPLE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env.example")

# Variable NAME shape that means "this holds a credential" -- these must
# always be empty in .env.example. Deliberately not "every variable must
# be empty": a template file legitimately documenting a safe non-secret
# default (USE_SYSTEM_ONE=false, say) is normal and not what leaked here
# -- narrowing to credential-shaped names is what lets this test catch
# the real danger (an API key) without also flagging every ordinary
# config default as a false positive, which would just train someone to
# ignore this test's failures.
_CREDENTIAL_NAME_RE = re.compile(r"(_KEY|_TOKEN|_SECRET|_PASSWORD|_CREDENTIAL)$")


class TestEnvExampleHasNoRealValues(unittest.TestCase):
    def test_every_credential_variable_is_empty(self):
        with open(ENV_EXAMPLE_PATH, encoding="utf-8") as f:
            lines = f.readlines()

        violations = []
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            if _CREDENTIAL_NAME_RE.search(key) and value.strip():
                violations.append(f"line {lineno}: {key} has a non-empty value")

        self.assertEqual(
            violations, [],
            ".env.example must never hold a real credential value -- found:\n"
            + "\n".join(violations) + "\nMove the real value to .env (gitignored) "
            "and restore this file to KEY= with nothing after it.",
        )

    def test_no_line_contains_a_recognizable_secret_shape(self):
        # Defense in depth beyond name-matching: catch a real-looking
        # secret even on a line whose variable name doesn't end in one of
        # the credential suffixes above (a typo'd name, a new provider
        # added without following the _KEY convention, etc).
        secret_shape = re.compile(
            r"nvapi-[A-Za-z0-9_-]{20,}"
            r"|sk-[A-Za-z0-9]{20,}"
            r"|AIza[A-Za-z0-9_-]{20,}"
            r"|[A-Za-z0-9]{32,}\.[A-Za-z0-9_-]{16,}"  # ollama-style id.secret
        )
        with open(ENV_EXAMPLE_PATH, encoding="utf-8") as f:
            content = f.read()
        match = secret_shape.search(content)
        if match is not None:
            self.fail(f"a real-looking secret was found in .env.example: {match.group(0)[:12]!r}...")


if __name__ == "__main__":
    unittest.main()
