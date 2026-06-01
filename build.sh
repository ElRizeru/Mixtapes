#!/usr/bin/bash

python -m venv --system-site-packages .venv
source .venv/bin/activate
pip install nuitka

# build with nuitka
nuitka --clang --include-package=ui --include-package=api --include-package=player --include-module=logger --output-filename=mixtapes src/main.py 

deactivate