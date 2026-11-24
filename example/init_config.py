"""Create a separate example config; never overwrite an existing key or file."""

import argparse
import json
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    config = json.loads(
        Path(__file__).with_name("config.example.json").read_text("utf-8")
    )
    config["keys"]["router"] = "sk-" + secrets.token_urlsafe(32)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print("Keeping existing configuration; no key changed.")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(config, file, indent=2)
        file.write("\n")
    print(f"Created {args.output}; key is stored in this file, not printed.")


if __name__ == "__main__":
    main()
