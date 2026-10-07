"""Use UTF-8 for CLI output, including redirected output on Windows."""
import sys


def configure_output():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
