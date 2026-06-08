#!/usr/bin/bash

python -m venv .venv
source .venv/bin/activate
pip install nuitka
VENV_SITE_PACKAGES=$(python -c "import sysconfig; print(sysconfig.get_path('purelib'))")
deactivate

export PYTHONPATH="$VENV_SITE_PACKAGES:$PYTHONPATH"
cd src

# build with nuitka
python -m nuitka --clang \
--file-reference-choice=runtime \
--include-package=ui \
--include-package=api \
--include-package=player \
--include-module=logger \
--output-filename=mixtapes \
main.py