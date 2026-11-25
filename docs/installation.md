# Installation

## Environment

This tree contains eleven Python modules and no dependency manifest, package metadata, CLI, bundled Kivi checkout, or tests. Run Python from the repository directory so its imports resolve. When invoking it elsewhere, put this directory on `PYTHONPATH`.

The source uses Python 3.10-era syntax such as `str | Path` and built-in generic types. Python 3.10 is a syntax floor, not a tested support range. The documentation example was executed with Python 3.14.10, PyYAML 6.0.3, and Z3 5.3.1. No broader compatibility guarantee is established.

| Dependency | Evidence and use |
| --- | --- |
| `yaml` / PyYAML | Imported by the builder, event compiler, specification resolver, and materializer |
| `z3` / z3-solver | Imported by `beek_synthesis.py`; uses `Optimize`, finite integer domains, and lexicographic minimization |
| Kivi source | Imported at verification time from `kivi_root/kivi` |
| Bundled SPIN executable | Invoked from `kivi_root/libs/Spin/Src/spin` |
| `gcc` on `PATH` | Kivi compiles generated `pan.c` |
| Writable Kivi working directories | Kivi generates PROMELA, C, executables, and results |

The remaining direct Beek imports are from the standard library or the other supplied modules. The inspected external Kivi checkout declares `PyYAML==6.0`; that is Kivi metadata, not a Beek requirement. Its code also imports local Kivi modules. Use that checkout's own dependency instructions when preparing it.

If PyYAML or Z3 is missing, install these confirmed imports in a virtual environment. This command does not install Beek:

```bash
python3 -m venv /tmp/beek-env
source /tmp/beek-env/bin/activate
python3 -m pip install PyYAML z3-solver
```

This installation block was not executed during documentation validation because both libraries were already present. It specifies no dependency versions; inspect your resolved versions with the checks below.

## Kivi layout

The upstream identity was confirmed from the local checkout's Git remote: [bingzheliu/Kivi](https://github.com/bingzheliu/Kivi). The inspected checkout was at `81e6c253573b0cc41727885948c1240892d1a6e2`, the revision recorded in the manuscript. The local checkout has experimental changes; a pristine upstream checkout was not independently tested here.

Supply `kivi_root` explicitly. Beek does not discover it or read a built-in environment variable. This layout is required by the adapter and the inspected Kivi code:

```text
Kivi/
  kivi/
    verifier_operator.py
    util.py
    config.py
    model_generator.py
    templates/
  libs/Spin/Src/spin
  bin/                       # Beek changes cwd here during verification
  temp/                      # must already exist for Kivi's directory helper
  results/                   # Kivi can create this
```

Keep a dedicated writable copy for Beek. Verification inserts `kivi_root/kivi` into `sys.path`, imports `util` and `verifier_operator`, temporarily sets Kivi options, and changes the process working directory to `bin`. It restores the working directory and the flags it changed. The imported modules remain cached. Use a fresh Python process when changing Kivi roots; run campaigns sequentially. See [verification](verification.md).

## Check availability

From the repository directory, select your existing writable Kivi checkout:

```bash
read -r -p "Absolute path to a writable Kivi checkout: " BEEK_KIVI_ROOT
export BEEK_KIVI_ROOT
python3 -B - <<'PY'
import os
import shutil
import sys
from pathlib import Path
import yaml
import z3
import beek_cegis
import beek_model_builder
import beek_synthesis

root = Path(os.environ["BEEK_KIVI_ROOT"]).resolve(strict=True)
print("Python:", sys.version)
print("PyYAML:", yaml.__version__, "Z3:", z3.get_version_string())
for relative in ("kivi/verifier_operator.py", "libs/Spin/Src/spin", "bin", "temp"):
    assert (root / relative).exists(), relative
assert os.access(root / "libs/Spin/Src/spin", os.X_OK)
assert shutil.which("gcc"), "gcc is missing from PATH"
print("Layout and direct imports OK:", root)
PY
"$BEEK_KIVI_ROOT/libs/Spin/Src/spin" -V
gcc --version
```

`BEEK_KIVI_ROOT` is a shell variable used by these examples only. No `spin` command on `PATH` is required: the code uses the bundled absolute path. No Docker, Kind, kubectl, cluster credentials, or workload image is required for a formal Beek campaign.

The manuscript's Table 5.3 records an experimental environment, including SPIN 6.5.2 and Python 3.14.10. It does not define repository installation constraints. The local bundled SPIN used for validation reported 6.5.3. Historical operational versions are recorded in [evaluation](evaluation.md).

Continue with the [quickstart](quickstart.md).
