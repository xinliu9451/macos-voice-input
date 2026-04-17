"""Entry point so that `python -m voice_input` runs the application."""

import sys

from voice_input.app import main


if __name__ == "__main__":
    sys.exit(main())
