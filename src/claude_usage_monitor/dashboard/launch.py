"""Console-script shim: `claude-usage-dashboard` -> `streamlit run app.py`."""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    app_path = Path(__file__).with_name("app.py")
    try:
        from streamlit.web import cli as stcli
    except ModuleNotFoundError:
        print(
            "The dashboard extra is not installed. Run:\n"
            "  uv sync --extra dashboard\n"
            "then:\n"
            "  uv run --extra dashboard claude-usage-dashboard",
            file=sys.stderr,
        )
        return 1
    sys.argv = ["streamlit", "run", str(app_path), *argv]
    return stcli.main()  # type: ignore[no-any-return]


if __name__ == "__main__":
    raise SystemExit(main())
