"""Resolve the sandbox terminal account without applying caller environment."""

import json
import pwd

account = pwd.getpwnam("daytona")
if account.pw_uid == 0:
    raise PermissionError("Daytona terminals require a non-root daytona account")
print(json.dumps([account.pw_dir, account.pw_shell]))
