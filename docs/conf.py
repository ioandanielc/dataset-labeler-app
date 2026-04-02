# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import sys
from pathlib import Path

# Make the project root importable so autodoc can find the labeler package.
sys.path.insert(0, str(Path(__file__).parent.parent))

# -- Project information -----------------------------------------------------

project = 'Melt Pool Frame Labeler'
copyright = '2026, Dataset Labeler'
author = 'Dataset Labeler'
release = '0.1.0'

# -- General configuration ---------------------------------------------------

extensions = [
    'sphinx.ext.autodoc',       # reads docstrings from source code automatically
    'sphinx.ext.napoleon',      # understands Google-style and NumPy-style docstrings
    'sphinx.ext.viewcode',      # adds [source] links next to each function in the docs
]

# Napoleon settings — tell it we use Google style
napoleon_google_docstring = True
napoleon_numpy_docstring = False

# autodoc settings — show members in source order, not alphabetical
autodoc_member_order = 'bysource'
autodoc_typehints = "description"   # move types from the signature into the Args table,
                                    # so the rendered HTML shows type + description
                                    # together — easier to read at a glance.

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

language = 'en'

# -- Options for HTML output -------------------------------------------------

html_theme = 'sphinx_rtd_theme'   # Read the Docs theme — clean and familiar
html_static_path = ['_static']
