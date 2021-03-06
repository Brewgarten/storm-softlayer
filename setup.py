"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-softlayer
This project is licensed under the MIT License, see LICENSE
"""
import sys

from setuptools import setup, find_packages

import versioneer


needs_pytest = {"pytest", "test", "ptr", "coverage"}.intersection(sys.argv)
pytest_runner = ["pytest-runner"] if needs_pytest else []

setup(
    author = "IBM",
    author_email = "",
    cmdclass=versioneer.get_cmdclass(),
    description = "Cloud infrastructure driver for SoftLayer",
    entry_points = {
        "console_scripts" : [
            "slcli = storm.drivers.softlayer:slcli"
        ]
    },
    install_requires = [
        "apache-libcloud",
        "SoftLayer==5.1"
    ],
    keywords = "python storm cloud",
    license = "MIT",
    name = "storm-softlayer",
    packages = find_packages(),
    url = "",
    setup_requires=[] + pytest_runner,
    tests_require=["pytest", "pytest-cov"],
    version = versioneer.get_version(),
)
