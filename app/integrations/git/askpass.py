"""Git-only credential helper; credentials are supplied through the child environment."""

import os
import sys


def main() -> None:
    prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if "username" in prompt:
        print("x-access-token")
    elif "password" in prompt:
        print(os.environ.get("PLATFORM_GIT_TOKEN", ""))
    else:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
