# PerunChat - Build & test pipeline

Extracted verbatim from `AGENTS.md` as part of the context-budget migration. Retrievable as `perunchat.context.build`.

## Build & test pipeline

```bash
# install in editable mode
pip install -e ".[dev]"

# run the test suite
python -m pytest tests/ -q

# syntax check
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('perunchat/**/*.py',recursive=True)+glob.glob('tests/**/*.py',recursive=True)]"
```
