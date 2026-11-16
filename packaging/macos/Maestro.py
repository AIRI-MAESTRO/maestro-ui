"""Native macOS application entry point for the Maestro desktop UI."""

from care.desktop import prepare_desktop_working_directory


if __name__ == "__main__":
    # Run before importing/calling ``care.cli.main``: CLI bootstrap reads the
    # workspace .env immediately, so the bundle must never start from its
    # sealed Contents/Resources directory (or Finder's ``/`` cwd).
    prepare_desktop_working_directory()
    from care.cli import main

    raise SystemExit(main(["app"]))
