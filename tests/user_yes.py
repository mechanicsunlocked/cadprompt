"""The user's two yeses, for the tests that are about something else.

A new profile approves nothing: the AI's code waits for Run or Skip, and no
service outside the computer gets data before the user said Allow. A test of
the engine, the bridge or the panel that is not about these two questions
gives the yes here, in its own scratch profile (FREECAD_USER_HOME), the same
way the user gives it: the parameter AutoApprove, a record in consent.py.

The questions themselves are tested in test_approval_gui.py and
test_consent_gui.py, with a profile that has neither.
"""


def run_without_asking(on=True):
    """Settings > "Run the AI's code without asking"."""
    from cadprompt import approval
    approval.set_auto(on)


def allow(kind, url):
    """The user said Allow to this service: kind "openai", "anthropic", "account" or "custom"."""
    from cadprompt import consent
    consent.allow(kind, url)


def allow_claude_code(endpoint=""):
    """The user said Allow to Claude Code with the Claude account, and, with `endpoint`,
    to Claude Code for that other server."""
    from cadprompt import backends
    allow("account", backends.ANTHROPIC_URL)
    if endpoint:
        allow("custom", endpoint)
