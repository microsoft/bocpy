import re
from pathlib import Path

from setuptools import Extension, setup

# Load the README and strip any sections marked as PyPI-skip. GitHub still
# renders the original; PyPI's long_description gets the filtered version so
# unsupported content (e.g. Mermaid code blocks) does not appear as raw text.
_readme = Path(__file__).parent.joinpath("README.md").read_text(encoding="utf-8")
_readme = re.sub(
    r"<!-- pypi-skip-start -->.*?<!-- pypi-skip-end -->\n?",
    "",
    _readme,
    flags=re.DOTALL,
)

setup(
    long_description=_readme,
    long_description_content_type="text/markdown",
    ext_modules=[
        Extension(
            name="bocpy._core",
            sources=["src/bocpy/_core.c"],
        ),
        Extension(
            name="bocpy._math",
            sources=["src/bocpy/_math.c"],
        ),

    ]
)
