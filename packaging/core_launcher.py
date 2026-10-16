"""PyInstaller entry point; dispatch frozen worker processes before app imports."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from worldprompt_core.__main__ import main
    raise SystemExit(main())
