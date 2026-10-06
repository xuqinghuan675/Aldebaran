# Contributing to Aldebaran

Contributions are welcome.

## Development setup

Use Python 3.11 or 3.12 on Windows for the closest match to the primary desktop environment.

~~~powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
~~~

Run the application with:

~~~powershell
python app.py
~~~

## Before submitting a change

Run at least:

~~~powershell
python -m compileall -q app.py core ui tools
python tools/_test_mytt_cleanroom.py
python tools/_test_credentials.py
python tools/_test_runtime_paths.py
python tools/_test_release_hardening.py
python tools/_test_scrapling_runtime.py
~~~

Changes touching a specific subsystem should also run its nearby _test_*.py files.

## Pull requests

Keep pull requests focused and explain:

1. what changed;
2. why the change is needed;
3. what tests were run;
4. any behavior, compatibility, or migration impact.

Do not include generated runtime data, caches, screenshots containing account information, real portfolios, private documents, API keys, tokens, or machine-specific absolute paths.

## Third-party code policy

Do not paste or vendor third-party source unless its license is known and compatible with redistribution.

When adding vendored code or assets:

1. retain required copyright and license notices;
2. add the component to THIRD_PARTY_NOTICES.md;
3. document its upstream project and version;
4. avoid describing a component as MIT, Apache-2.0, GPL, or another license unless the upstream source actually provides that license.

If only an algorithm or public specification is needed, prefer an independent implementation over copying source text.

## Style

Prefer readable, deterministic code and explicit fallbacks. Preserve the repository's privacy boundary: code belongs in Git; user data and credentials do not.
