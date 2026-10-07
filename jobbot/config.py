"""Relative profile paths are relative to the TOML file, not the current directory."""
from pathlib import Path
import tomllib


def load_config(path):
    path=Path(path).expanduser().resolve()
    config=tomllib.loads(path.read_text(encoding='utf-8-sig'))
    applications=config.get('applications',{})
    if applications.get('profile_dir'):
        directory=Path(applications['profile_dir']).expanduser()
        applications['profile_dir']=str((path.parent/directory).resolve())
    return config
