"""Reject architecture/emulation and Python patch drift before packaging."""
import json
import os
import platform
import struct
import sys

target = os.environ['EXPECTED_TARGET']
actual = (platform.system(), platform.machine().lower(), struct.calcsize('P') * 8)
expected = {'macos-arm64': ('Darwin', 'arm64', 64), 'windows-x64': ('Windows', 'amd64', 64)}[target]
print(json.dumps({'system': actual[0], 'architecture': actual[1], 'python_bits': actual[2], 'python': platform.python_version()}))
if actual != expected or platform.python_version() != '3.12.13':
    sys.exit('Runner/interpreter does not match the explicit native build contract')
