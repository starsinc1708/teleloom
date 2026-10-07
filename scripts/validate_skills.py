"""Validate portable skill metadata, reference locality, and bundled completeness."""

import re
from pathlib import Path

import yaml


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "skills"
    skills = sorted(root.glob("*/SKILL.md"))
    if len(skills) != 6:
        raise SystemExit("Expected six skills")
    for path in skills:
        text = path.read_text(encoding="utf-8")
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise SystemExit(f"Missing YAML frontmatter: {path}")
        metadata = yaml.safe_load(parts[1])
        name = metadata.get("name", "")
        description = metadata.get("description", "")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or name != path.parent.name:
            raise SystemExit(f"Invalid name: {path}")
        if not 1 <= len(description) <= 1024:
            raise SystemExit(f"Invalid description: {path}")
        for reference in re.findall(r"\]\((references/[^)]+)\)", parts[2]):
            resolved = (path.parent / reference).resolve()
            if path.parent.resolve() not in resolved.parents or not resolved.is_file():
                raise SystemExit(f"Missing or external reference: {path}: {reference}")
        print(f"Valid: {name}")


if __name__ == "__main__":
    main()
