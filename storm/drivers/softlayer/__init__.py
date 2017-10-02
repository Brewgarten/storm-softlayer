"""
Copyright (c) IBM 2015-2017. All Rights Reserved.
Project name: storm-softlayer
This project is licensed under the MIT License, see LICENSE
"""
from pkgutil import extend_path

from ._version import get_versions
from .softlayerDriver import (DEFAULT_CPU_SIZE, DEFAULT_DISK_SIZE, DEFAULT_RAM_SIZE,
                              slcli,
                              SoftLayerNodeDriver, SoftLayerNodeLocation, SoftLayerNodeSize)


__path__ = extend_path(__path__, __name__)

__version__ = get_versions()['version']
del get_versions

__all__ = [
    "DEFAULT_CPU_SIZE", "DEFAULT_DISK_SIZE", "DEFAULT_RAM_SIZE",
    "slcli",
    "SoftLayerNodeDriver", "SoftLayerNodeLocation", "SoftLayerNodeSize"
]

