# Environment Version Compliance Check

`docs/env_inventory.py` checks `pip`, `certifi`, `uv` and `conda` across every
Python environment it can find on your machine, compares each one against the
required minimum versions, and writes a CSV report plus a ready-to-paste
report-back email body.

**Scope.** The script scans the whole machine — all fixed drives on Windows, the
filesystem root on Linux — covering conda installations and environments,
uv-managed Pythons, virtual environments and system Pythons. When run on Windows
it also probes any installed WSL distributions. Containers, CI/CD and build
environments, and live/deployed environments are *not* covered and must be
checked separately — see
[Out of scope](#out-of-scope--check-these-separately).

The script is **report-only**. It never upgrades or modifies an environment, and
writes only into the output directory.

## Required versions

| Package | Required |
| --- | --- |
| pip | >= 26.2.2 |
| certifi | >= 2026.10.19 |
| uv | >= 0.13.6 (where used) |
| conda | >= 26.9.4 (where used) |

These are the defaults built into the script and can be overridden with
`--min-pip`, `--min-certifi`, `--min-uv` and `--min-conda`.

## Prerequisites

- Any Python 3 interpreter. The script is standard-library only — you do **not**
  need to install `requirements.txt`.
- `conda` and `uv` are optional. If either is missing from `PATH`, that discovery
  step is skipped silently rather than failing.
- Runs on Windows and Linux. No `sudo`/admin required; unreadable directories are
  skipped rather than escalated.
- Run from the repository root so the default relative output path resolves.

## Quick start

```powershell
python docs/env_inventory.py
```

The default run scans the whole filesystem for virtual environments, which is
slow on first run. It is bounded by `--scan-timeout` (default 300s), so it always
finishes. For a fast re-check of the environments conda and uv already know
about:

```powershell
python docs/env_inventory.py --no-venv-scan
```

On Linux the invocation is identical:

```bash
python3 docs/env_inventory.py
```

## What it checks, and how

- **Discovery** — conda installations (`conda info --json` plus the well-known
  `miniforge3` / `anaconda3` / `miniconda3` locations), conda environments
  (`conda env list --json`, queried against *every* installation found), uv
  (`uv python list`), virtual environments (walks for `pyvenv.cfg`), system
  Pythons (`py -0p` and `PATH` on Windows; `/usr/bin`, `/usr/local/bin`, `/bin`,
  `/opt/*/bin` and pyenv on Linux), and WSL distributions (`wsl.exe -l -q`).
- **Interpreter layout** — both conda-style (`<env>/python.exe`) and venv-style
  (`<env>/Scripts/python.exe`, `<env>/bin/python`) layouts are handled.
- **Deduplication** — environments are keyed by resolved interpreter path, so the
  same interpreter found twice appears once. When that happens the `source` column
  shows the merged origins, e.g. `conda-root+conda+system`.
- **Probing** — each interpreter is run as a subprocess and asked for its own
  package metadata, so the versions reported are the ones that interpreter
  actually resolves. Probes run in parallel (`--max-workers`, default 8).
- **Scan safety** — the walk is depth-limited, prunes noisy directories and
  pseudo-filesystems (`/proc`, `/sys`, `node_modules`, `WinSxS`, …), guards
  against symlink and junction loops, and stops at `--scan-timeout`.

## Command reference

| Flag | Default | Purpose |
| --- | --- | --- |
| `--output-dir` | `data/reports` | Directory for the CSV and email body. |
| `--scan-root` | all fixed drives / `/` | Extra directory to scan for virtual environments. Repeatable. |
| `--max-depth` | `6` | Maximum directory depth for the virtual environment scan. |
| `--scan-timeout` | `300` | Seconds to spend scanning for virtual environments before stopping. |
| `--max-workers` | `8` | Interpreters to probe in parallel. `1` disables parallelism. |
| `--timeout` | `20` | Seconds allowed per interpreter probe. |
| `--min-pip` | `26.2.2` | Minimum acceptable pip version. |
| `--min-certifi` | `2026.10.19` | Minimum acceptable certifi version. |
| `--min-uv` | `0.13.6` | Minimum acceptable uv version. |
| `--min-conda` | `26.9.4` | Minimum acceptable conda version. |
| `--status` | `auto` | Headline status for the email: `auto`, `no-issue`, `updated` or `outstanding`. |
| `--email-out` | `<output-dir>/env_compliance_email_<timestamp>.txt` | Path for the report-back email body. |
| `--no-email` | off | Do not write the email body. |
| `--no-conda` | off | Skip conda installation and environment discovery. |
| `--no-uv` | off | Skip uv discovery. |
| `--no-venv-scan` | off | Skip the on-disk virtual environment scan. |
| `--no-system` | off | Skip system Python discovery. |
| `--no-wsl` | off | Skip WSL distribution discovery (Windows only). |
| `--print` | off | Also echo the CSV content to stdout. |

## Reading the output

Each run produces two files in the output directory:

- `env_inventory_<timestamp>.csv` — one row per environment.
- `env_compliance_email_<timestamp>.txt` — the report-back email body.

The CSV columns, in order, fall into groups:

| Group | Columns |
| --- | --- |
| Identity | `source`, `name`, `env_path`, `python_executable` |
| Interpreter | `python_version`, `implementation` |
| pip | `pip_version`, `pip_status` |
| certifi | `certifi_version`, `certifi_path`, `certifi_status` |
| uv | `uv_version`, `uv_status` |
| conda | `conda_version`, `conda_status` |
| Overall verdict | `env_verdict` |
| Host tooling | `conda_tool_version`, `uv_tool_version` |
| Probe result | `status`, `error` |

The `source` column records where the environment was found:

| Value | Meaning |
| --- | --- |
| `conda-root` | A conda installation root (miniforge, anaconda, miniconda). |
| `conda` | An environment registered with a conda installation. |
| `uv` | A uv-managed Python. |
| `venv` | A virtual environment found by the filesystem scan. |
| `system` | A system or `PATH` Python. |
| `wsl:<distro>` | An interpreter inside the named WSL distribution. |

An interpreter found by more than one method shows the merged sources, e.g.
`conda-root+conda+system`.

Status values:

| Value | Meaning |
| --- | --- |
| `ok` | Installed version meets the minimum. |
| `outdated` | Installed but below the minimum. Needs action. |
| `missing` | Not installed and expected to be (applies to pip). Needs action. |
| `n/a` | Not installed, and not required in every environment (certifi, uv, conda). |
| `unknown` | Version string could not be parsed, or the probe failed. |

Environment verdicts:

| Value | Meaning |
| --- | --- |
| `compliant` | Nothing outdated or missing. |
| `action_required` | At least one package is `outdated` or `missing`. |
| `probe_error` | The interpreter could not be run — see the `error` column. |

`conda_tool_version` and `uv_tool_version` are machine-level values and are
repeated on every row; they describe the host tools, not the individual
environment.

## Reporting workflow

1. **Run the check.**

   ```powershell
   python docs/env_inventory.py
   ```

2. **Review the results.** Open the CSV and filter `env_verdict`. Anything marked
   `action_required` needs remediation; anything marked `probe_error` needs a look
   at the `error` column (see [Troubleshooting](#troubleshooting)).

3. **Remediate** the affected environments — see
   [Remediating an environment](#remediating-an-environment).

4. **Re-run to confirm.** Do not assume an upgrade worked; re-run the script and
   check the verdicts have flipped to `compliant`.

5. **Generate the final report.** Once you have remediated:

   ```powershell
   python docs/env_inventory.py --status updated
   ```

6. **Send the report back.** Open the generated `.txt`, fill in every
   `<TO BE COMPLETED>` placeholder, and email it to datascience@ukkhsa.gov.uk.

The report-back status is one of three values:

| Status | When to use it |
| --- | --- |
| No issue found | Everything was already compliant. The script sets this automatically. |
| Updated | You found and fixed issues. Set it with `--status updated`. |
| Still outstanding | Issues remain. The script sets this automatically, and you must supply the blockers and an expected completion date. |

The script can determine "No issue found" and "Still outstanding" on its own, but
it has no way to know you have remediated something — hence `--status updated`.

## Remediating an environment

Always target the exact interpreter from the `python_executable` column so you
upgrade the environment you intend to.

Virtual environments and system Pythons:

```powershell
& "C:\path\to\env\Scripts\python.exe" -m pip install --upgrade pip certifi
```

On Linux the equivalent is:

```bash
/path/to/env/bin/python -m pip install --upgrade pip certifi
```

Conda environments:

```powershell
conda activate <env_name>
python -m pip install --upgrade pip certifi
```

For conda-managed builds you may prefer `conda update pip certifi` instead, to
keep the packages under conda's control.

The `conda` tool itself, run once per installation listed as `conda-root`:

```powershell
conda update -n base -c conda-forge conda
```

If you have more than one conda installation, each has its own `conda` version
and each needs updating separately — the CSV lists them as distinct `conda-root`
rows with their own `env_path`.

The `uv` tool itself:

```powershell
uv self update
```

If `uv` was installed with pip rather than the standalone installer, upgrade it
the same way as any other package: `python -m pip install --upgrade uv`.

After any change, re-run the script rather than assuming the upgrade took effect.

## Running on Linux

The script runs natively on Linux with no changes:

```bash
python3 docs/env_inventory.py
```

Differences from a Windows run:

- The scan starts at `/`, with `/opt`, `/srv`, `/usr/local`, `/home` and
  `/var/www` visited first so common locations are found quickly.
- System Pythons come from `/usr/bin`, `/usr/local/bin`, `/bin`, `/opt/*/bin`
  and `~/.pyenv/versions` rather than the `py` launcher.
- Interpreters resolve to `<env>/bin/python`.
- `/proc`, `/sys`, `/dev` and `/run` are never walked.
- **Running as an unprivileged user, other users' home directories are
  invisible.** The script will not escalate. If you need a machine-wide picture
  on a shared host, run it as a user that can read those paths, or run it once
  per user account.

## Out of scope — check these separately

The script only sees environments on the machine it runs on (plus its WSL
distributions). These must be checked by hand, and the generated email body has a
`<TO BE COMPLETED>` block for the results.

**Containers** — inspect the image rather than the host:

```powershell
docker run --rm <image> python -m pip list
```

Fix by updating the pinned versions in the `Dockerfile` or `requirements.txt`,
then rebuilding the image.

**CI/CD and build environments** — check the versions pinned in the pipeline
definition (e.g. workflow YAML, build scripts) and in any lockfiles or
`requirements.txt` the pipeline installs from. Updating the pin is the fix; the
next build picks it up.

**Live/deployed environments** — either run this script on the host, or inspect
the deployed package manifest for the service.

## Troubleshooting

**`probe_error` with "interpreter not found"** — usually a conda environment whose
directory has been deleted but which is still registered. Remove it with
`conda env remove -n <name>`, or ignore the row.

**`probe_error` on `python3`** — on Windows this is typically the Microsoft Store
alias stub rather than a real interpreter. Disable it under *Settings → Apps →
Advanced app settings → App execution aliases*, or ignore the row.

**certifi shows `ok` but the path is under `AppData\Roaming\Python\...`** — a
user-site install is shadowing the environment's own copy. Upgrading certifi
*inside* the environment will not change what the script reports, because the
user-site copy still wins on `sys.path`. Either uninstall the user-site copy
(`python -m pip uninstall certifi` with no environment active) or accept the
shadowed version.

**`uv_tool_version` is blank** — `uv` is not installed or not on `PATH`. This is
expected if you do not use uv, and is not counted as a failure.

**The venv scan is slow, or misses an environment** — the first system-wide run
is the slow one. If it is being cut short, raise `--scan-timeout`; if an
environment is nested deeply, raise `--max-depth`; if it lives somewhere unusual,
point `--scan-root` straight at it. For routine re-checks, `--no-venv-scan` is
much faster.

**`conda_status` is `n/a` on most rows** — expected. Only conda installations
(`conda-root`) and environments that have the `conda` package installed report a
conda version; ordinary environments do not and are not penalised.

**Several `conda-root` rows appear** — you have more than one conda installation.
Each carries its own conda version and must be updated separately.

**No `wsl:` rows appear** — either WSL is not installed, `--no-wsl` was passed,
or the distros failed to start. A distro that will not start produces a
`probe_error` row rather than failing the run.

**A probe times out** — raise `--timeout`. Slow network drives and antivirus
scanning are the usual causes. WSL probes are slower than local ones.

**`n/a` versus `missing`** — absence of certifi, uv or conda reports `n/a` and
does not count against the environment. Absence of pip reports `missing` and does.
