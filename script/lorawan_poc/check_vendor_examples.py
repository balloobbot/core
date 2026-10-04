"""Compare vendored models with library examples, ignoring import ordering."""

import argparse
import ast
from pathlib import Path


def normalized(path: Path) -> str:
    """Ignore import sorting differences between the two repository styles."""
    tree = ast.parse(path.read_text())
    imports = [
        node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    other = [
        node for node in tree.body if not isinstance(node, ast.Import | ast.ImportFrom)
    ]
    tree.body = sorted(imports, key=ast.dump) + other
    return ast.dump(tree)


def main() -> None:
    """Fail when a model or CLI differs from its tested library example."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "library", type=Path, help="Path to the lorawan-connection checkout"
    )
    args = parser.parse_args()
    core = Path(__file__).resolve().parents[2]
    for vendor in ("sensecap", "dragino"):
        for name in ("__init__.py", "__main__.py"):
            example = args.library / "examples" / f"{vendor}_lorawan" / name
            vendored = (
                core
                / "homeassistant/components"
                / vendor
                / "_vendor"
                / f"{vendor}_lorawan"
                / name
            )
            if normalized(example) != normalized(vendored):
                parser.error(f"Vendored example differs: {vendored}")
    print("Vendored examples match (ignoring import ordering)")


if __name__ == "__main__":
    main()
