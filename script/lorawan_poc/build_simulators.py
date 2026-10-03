"""Build radio test binaries from the pinned simulator checkout without modifying it."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    """Add a dynamic payload callback with a Go source overlay for the relay test."""
    checkout, output = (Path(arg).resolve() for arg in sys.argv[1:])
    source = checkout / "simulator/device.go"
    text = source.read_text()
    changes = [
        ("payload []byte", "payload []byte\n\tpayloadFunc func() []byte"),
        (
            "func (d *Device) dataUp() {",
            "func (d *Device) dataUp() {\n\tpayload := d.payload\n\tif d.payloadFunc != nil { payload = d.payloadFunc() }",
        ),
        ("Bytes: d.payload,", "Bytes: payload,"),
    ]
    for old, new in changes:
        if text.count(old) != 1:
            raise SystemExit("Simulator source differs from the documented revision")
        text = text.replace(old, new)
    text += """
func WithUplinkPayloadFunc(f func() []byte) DeviceOption {
    return func(d *Device) error { d.payloadFunc = f; return nil }
}
"""
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        patched = Path(temporary) / "device.go"
        patched.write_text(text)
        overlay = Path(temporary) / "overlay.json"
        overlay.write_text(json.dumps({"Replace": {str(source): str(patched)}}))
        for name in ("simulator", "simulator_dragino"):
            subprocess.run(
                [
                    "go",
                    "build",
                    "-overlay",
                    str(overlay),
                    "-o",
                    str(output / name),
                    str(Path(__file__).parent.resolve() / f"{name}.go"),
                ],
                cwd=checkout,
                check=True,
            )


if __name__ == "__main__":
    main()
