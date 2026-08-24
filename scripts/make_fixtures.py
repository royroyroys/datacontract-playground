"""Regenerate all catalogue fixtures: python -m scripts.make_fixtures"""
from pathlib import Path

from playground.fixtures import generate_all


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    written = generate_all(root / "contracts")
    for path in written:
        print(f"wrote {path.relative_to(root)}")


if __name__ == "__main__":
    main()
