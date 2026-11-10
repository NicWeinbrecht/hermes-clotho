"""Add a custom provider entry to a Hermes config.yaml without a second top-level `providers:` key.

    python3 eval/add_provider.py <config.yaml> <name> <port>

Text-level edit (stdlib only, no YAML library): if the file already has a top-level `providers:`
block (another tool may have written one), the entry goes inside it; otherwise a new block is
appended. A duplicate `providers:` key makes Hermes refuse the whole file ("could not parse plugin
selection"). Exit 0 when the entry is present afterwards; 2 when the name exists with another port.
"""
import re
import sys


def add_provider(text: str, name: str, port: int) -> str:
    api = f"http://127.0.0.1:{port}/v1"
    entry = f"  {name}:\n    api: {api}\n    models:\n      scripted: {{}}\n"
    lines = text.splitlines(keepends=True)
    if any(re.match(rf"^  {re.escape(name)}:\s*$", ln) for ln in lines):
        if api not in text:
            raise ValueError(f"provider {name} exists with another endpoint")
        return text
    for i, ln in enumerate(lines):
        if re.match(r"^providers:\s*$", ln):
            return "".join(lines[:i + 1]) + entry + "".join(lines[i + 1:])
        if re.match(r"^providers:\s*\{\s*\}\s*$", ln):
            return "".join(lines[:i]) + "providers:\n" + entry + "".join(lines[i + 1:])
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "providers:\n" + entry


def main(argv):
    path, name, port = argv[1], argv[2], int(argv[3])
    with open(path) as f:
        text = f.read()
    try:
        new = add_provider(text, name, port)
    except ValueError as exc:
        print(f"add_provider: {exc}; fix {path} by hand", file=sys.stderr)
        return 2
    if new != text:
        with open(path, "w") as f:
            f.write(new)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
