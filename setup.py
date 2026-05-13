#!/usr/bin/env python3
"""Setup for cli-anything-nightscout."""

from pathlib import Path
from setuptools import setup, find_namespace_packages

ROOT = Path(__file__).parent
README = ROOT / "cli_anything/nightscout/README.md"

long_description = README.read_text(encoding="utf-8") if README.exists() else ""

setup(
    name="cli-anything-nightscout",
    version="1.2.1",
    description="CLI harness for the Nightscout CGM remote monitor — REST API v1 + v3",
    long_description=long_description,
    long_description_content_type="text/markdown",

    author="cli-anything",
    url="https://github.com/HKUDS/CLI-Anything",

    project_urls={
        "Source": "https://github.com/HKUDS/CLI-Anything",
        "Nightscout": "https://github.com/nightscout/cgm-remote-monitor",
        "Tracker": "https://github.com/HKUDS/CLI-Anything/issues",
    },

    license="MIT",

    packages=find_namespace_packages(include=["cli_anything.*"]),

    python_requires=">=3.10",

    install_requires=[
        "click>=8.1",
        "prompt-toolkit>=3.0",
        "requests>=2.28",
    ],

    extras_require={
        "dev": [
            "pytest>=7",
            "pytest-cov>=4",
        ],
    },

    entry_points={
        "console_scripts": [
            "cli-anything-nightscout=cli_anything.nightscout.nightscout_cli:main",
        ],
    },
    package_data={
        "cli_anything.nightscout": ["skills/*.md"],
    },
    include_package_data=True,
    zip_safe=False,

    keywords=[
        "cli",
        "nightscout",
        "cgm",
        "diabetes",
        "glucose",
        "cli-anything",
    ],

    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Healthcare Industry",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
)
