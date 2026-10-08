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
    for vendor in ("sensecap", "dragino", "milesight"):
        for example in (args.library / "examples" / f"{vendor}_lorawan").glob("*.py"):
            name = example.name
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
    captures = Path("tests/fixtures/device_uplinks")
    for fixture in (args.library / captures).glob("*.json"):
        vendored = (
            core / "tests/components/lorawan/fixtures/device_uplinks" / fixture.name
        )
        if fixture.read_bytes() != vendored.read_bytes():
            parser.error(f"Captured uplink differs: {vendored}")
    print("Vendored libraries and captured uplinks match (ignoring import ordering)")


if __name__ == "__main__":
    main()
