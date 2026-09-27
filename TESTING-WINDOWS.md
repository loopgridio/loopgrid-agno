# Windows validation

From the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev,e2e]"
python -m pytest -v
python -m compileall -q src examples tests
```

Start LoopGrid Core:

```powershell
docker start loopgrid-dify-test
Invoke-RestMethod http://127.0.0.1:8000/ready
```

For the real Core E2E, use a fresh workspace so verification is isolated from older test chains. The release gate is:

```text
evidence_complete
coverage = 100
verify.valid = true
failures = []
```
