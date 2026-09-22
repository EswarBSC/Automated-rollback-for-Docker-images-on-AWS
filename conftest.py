"""
Empty on purpose.

pytest puts the directory containing the top-most conftest.py on sys.path, so
this file is what lets `from app.main import app` work when you run plain
`pytest` from the repository root. Without it you would get ModuleNotFoundError.
"""
