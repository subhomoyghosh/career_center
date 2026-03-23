import os
import sys

# So job_finder can be found when run without installing the package (e.g. `streamlit run app.py`)
_root = os.path.dirname(os.path.abspath(__file__))
_src = os.path.join(_root, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from job_finder.ui_app import render_app

render_app()