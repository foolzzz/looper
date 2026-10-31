"""Carry the source installation's runtime dependency identity into a wheel."""
from pathlib import Path
from runpy import run_path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).parent
runtime_fingerprint = run_path(str(ROOT / "looper/components.py"))["runtime_fingerprint"]


class BuildPy(build_py):
    def run(self):
        super().run()
        record = Path(self.build_lib) / "looper/.runtime-fingerprint"
        record.write_text(runtime_fingerprint(ROOT) + "\n")


setup(cmdclass={"build_py": BuildPy})
