from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parent.parent


def load_module(module_name: str, relative_path: str):
    path = ROOT / relative_path
    module_dir = str(path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)

    spec = importlib.util.spec_from_file_location(module_name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
