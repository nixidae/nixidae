"""SONiC over its serial console: a Linux login shell, framed by `shell`.

SONiC VS is Debian with the SONiC switch stack. The host logs in on the
serial console and runs commands through `shell.frame`. The login and the
prompt are the only SONiC-specific parts; the framing is generic.

docs/design/console-guests.md has the measurements.
"""

from __future__ import annotations

import re
from typing import Final

from . import shell
from .control import Dialect, Rule

USER: Final = "admin"
PASSWORD: Final = "YourPaSsWoRd"
"""The community VS image's built-in admin credentials. A lab secret: the
guest has no network the host does not give it."""

PROMPT: Final = re.compile(rb"\r?\n" + re.escape(USER.encode()) + rb"@[\w.-]+:[^\r\n]*[$#] $")

# `(?<!Last )`: `login(1)` prints `Last login: ...` in the MOTD after the
# password. Without the guard a read ending on that colon-space types the
# user name into the shell.
LOGIN: Final = (
    Rule(re.compile(rb"(?<!Last )[Ll]ogin: $"), f"{USER}\r".encode()),
    Rule(re.compile(rb"[Pp]assword: $"), f"{PASSWORD}\r".encode()),
    Rule(PROMPT, None),
)

DIALECT: Final = Dialect(
    name="sonic",
    login=LOGIN,
    prompt=PROMPT,
    frame=shell.frame,
    parse=shell.parse,
)
