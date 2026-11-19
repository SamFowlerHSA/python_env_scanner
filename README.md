# Environment Version Compliance Check

This repository contains a report-only scanner for Python environments. The
`env_scanner` package (in `src/`) looks for installed conda, uv, virtualenv,
system, and WSL Python interpreters, probes each one, compares the detected
package versions against minimum thresholds, and writes a CSV plus a ready-to-paste
email summary.

It never installs or upgrades anything; it only inspects and writes reports.

## What the repo does

- Discovers Python environments on the current machine.
- Handles conda installations and conda-managed envs.
- Handles uv-managed interpreters.
- Finds venvs by scanning the filesystem for `pyvenv.cfg` files.
- Finds system Pythons and Windows/WSL Python locations.
- Deduplicates the same interpreter when it is discovered more than once.
- Probes each interpreter in-process to read its actual installed packages.
- Compares versions to minimum requirements.
- Writes a timestamped CSV report and a plain-text compliance email body.

This is a machine-local audit utility, not an environment manager. It does not
change `pip`, `certifi`, `uv`, or `conda` for you.

## Repository layout

```text
python_env_scanner/
├── src/env_scanner/
│   ├── inventory.py        # discovery, probing, compliance, reports, CLI
│   ├── aws.py              # optional AWS discovery (needs boto3)
│   └── __main__.py         # python -m env_scanner
├── tests/                  # unit and integration tests
├── docs/
│   └── AWS_AUTH_GUIDE.md   # AWS authentication and IAM permissions
├── env_inventory.py        # launcher so the scanner runs without installing
├── pyproject.toml          # packaging, pytest and ruff config
├── data/
│   └── reports/            # generated CSVs and email bodies
├── configs/
│   └── pipeline_config.yaml # repo config file (not required to run the scanner)
└── ...
```

The default output directory is `data/reports`, and it is created automatically
when the script runs.

## Script usage

Run the scanner from the repo root (no install needed):

```bash
python env_inventory.py
```

Or install it (`pip install -e .`, or `pip install -e ".[aws,dev]"` for AWS and
test tooling) and use either entry point:

```bash
env-scanner
python -m env_scanner
```

Run the tests with `python -m pytest` (or
`PYTHONPATH=src python -m unittest discover -s tests -t .`).

On Windows PowerShell the same command is:

```powershell
python .\env_inventory.py
```

Fast re-check without the full filesystem venv walk:

```bash
python env_inventory.py --no-venv-scan
```

Generate a final compliance email after remediation:

```bash
python env_inventory.py --status updated
```

## Required minimum versions

| Package | Required |
| --- | --- |
| `pip` | >= 26.2.2 |
| `certifi` | >= 2026.10.19 |
| `uv` | >= 0.13.6 (where used) |
| `conda` | >= 26.9.4 (where used) |

These are built in as defaults and can be overridden with:

- `--min-pip`
- `--min-certifi`
- `--min-uv`
- `--min-conda`

## Discovery and scan behaviour

The script intentionally scans broadly and is built to be resilient:

- On Windows it checks fixed drives and also inspects WSL distributions.
- On Linux it starts from the filesystem root and prioritises common paths such as
  `/opt`, `/srv`, `/usr/local`, `/home`, and `/var/www`.
- It skips noisy directories and pseudo-filesystems such as `/proc`, `/sys`,
  `node_modules`, `WinSxS`, `.git`, and similar.
- It respects a max scan depth and a timeout and will stop gracefully with partial
  results instead of hanging forever.
- It deduplicates environments by resolved interpreter path and merges the source
  labels when the same interpreter is found via multiple discovery methods.

This means the script is designed to be useful on a real workstation, even when it
finds several unrelated conda or venv environments from different projects.

## Command options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--output-dir` | `data/reports` | Directory for generated files |
| `--scan-root` | none | Extra directory to scan for venvs; repeatable |
| `--max-depth` | `6` | Directory depth limit for the venv scan |
| `--scan-timeout` | `300` | Max seconds to spend scanning venvs |
| `--max-workers` | `8` | Parallel probe workers |
| `--timeout` | `20` | Max seconds per interpreter probe |
| `--min-pip` | `26.2.2` | Minimum required pip version |
| `--min-certifi` | `2026.10.19` | Minimum required certifi version |
| `--min-uv` | `0.13.6` | Minimum required uv version |
| `--min-conda` | `26.9.4` | Minimum required conda version |
| `--status` | `auto` | Email headline: `auto`, `no-issue`, `updated`, `outstanding` |
| `--email-out` | `<output-dir>/env_compliance_email_<timestamp>.txt` | Custom email output path |
| `--no-email` | off | Skip writing the email body |
| `--no-conda` | off | Skip conda discovery |
| `--no-uv` | off | Skip uv discovery |
| `--no-venv-scan` | off | Skip filesystem venv scan |
| `--no-system` | off | Skip system Python discovery |
| `--no-wsl` | off | Skip WSL discovery on Windows |
| `--print` | off | Echo the generated CSV to stdout |

## Output files

Each run writes two files into `data/reports` unless a different output directory is
provided:

- `env_inventory_<timestamp>.csv` — one row per discovered environment.
- `env_compliance_email_<timestamp>.txt` — plain text report-back email body.

The CSV columns include:

- `source`, `name`, `env_path`, `python_executable`
- `python_version`, `implementation`
- `pip_version`, `pip_status`
- `certifi_version`, `certifi_path`, `certifi_status`
- `uv_version`, `uv_status`
- `conda_version`, `conda_status`
- `env_verdict`
- `conda_tool_version`, `uv_tool_version`
- `status`, `error`

The `source` values are usually one of:

- `conda-root`
- `conda`
- `uv`
- `venv`
- `system`
- `wsl:<distro>`

When the same interpreter is found more than once, the script merges the sources as
`conda-root+conda` or similar.

## Status and verdict values

### Per-package status

| Value | Meaning |
| --- | --- |
| `ok` | Installed version meets the minimum |
| `outdated` | Installed but below the minimum |
| `missing` | Required package is absent |
| `n/a` | Package is optional in this environment |
| `unknown` | Version could not be parsed or the probe failed |

### Environment verdict

| Value | Meaning |
| --- | --- |
| `compliant` | No failing package versions |
| `action_required` | At least one installed package is outdated or missing |
| `probe_error` | The interpreter could not be queried successfully |

## Reporting workflow

1. Run the scanner.
2. Review the CSV and filter on `env_verdict`.
3. Fix any environments marked `action_required` or `probe_error`.
4. Re-run the scanner to confirm the issue is resolved.
5. If you have remediated the problems, run:

   ```bash
   python env_inventory.py --status updated
   ```

6. Open the generated email file, fill in the required placeholders, and send it
   to the configured recipient.

The email body is intentionally plain text and includes placeholders such as:

- `Blockers: <TO BE COMPLETED>`
- `Expected completion date: <TO BE COMPLETED>`
- `Containers, CI/CD and deployed environments were checked separately: <TO BE COMPLETED>`

## Remediation guidance

The script does not perform upgrades itself. To remediate a target environment, use
that environment's exact `python_executable` path from the CSV.

Virtual environment or system Python:

```powershell
& "C:\path\to\env\Scripts\python.exe" -m pip install --upgrade pip certifi
```

Linux equivalent:

```bash
/path/to/env/bin/python -m pip install --upgrade pip certifi
```

Conda environment:

```powershell
conda activate <env_name>
python -m pip install --upgrade pip certifi
```

For conda-managed installs, `conda update pip certifi` may be preferable when you
want to keep the packages under conda management.

The host `conda` tool itself can be updated separately with:

```powershell
conda update -n base -c conda-forge conda
```

The host `uv` tool itself can be updated with:

```powershell
uv self update
```

After any change, re-run the scanner rather than assuming the upgrade took effect.

## Scope and non-scope

The script is specifically for local machine inspection. It does not cover:

- containers
- CI/CD pipelines
- build agents
- deployed production environments

Those should be checked separately and are intentionally left for a manual section
in the generated email.

## Troubleshooting

- `probe_error` with `interpreter not found`: the environment path exists in the
  registry but the interpreter has been removed.
- `probe_error` on `python3`: this can be a Windows Store alias stub instead of a
  real interpreter.
- `certifi` may appear `ok` even when the actual import path is under
  `AppData\Roaming\Python`; a user-site shadow copy is winning on `sys.path`.
- `uv_tool_version` blank is normal when `uv` is not installed or not on `PATH`.
- The full venv scan can be slow on the first run; use `--no-venv-scan` for a
  faster repeat check.
- `conda_status` being `n/a` on most rows is expected unless the environment has
  the `conda` package installed.

The script is deliberately conservative: unreadable directories are skipped, and it
fails gracefully rather than crashing the whole scan.

## AWS environment inventory

`--aws` adds an inventory of AWS workloads that run Python: EC2 instances (running),
Lambda functions, ECS task containers and SageMaker notebooks, training jobs and
endpoints. Requires `pip install boto3` and AWS credentials; see
[AWS_AUTH_GUIDE.md](docs/AWS_AUTH_GUIDE.md) for authentication and IAM permissions.

```bash
python env_inventory.py --aws                                  # us-east-1
python env_inventory.py --aws --aws-region eu-west-1 --aws-region us-west-2
python env_inventory.py --aws --no-aws-ec2 --no-aws-ecs        # Lambda + SageMaker only
python env_inventory.py --aws --no-conda --no-uv --no-venv-scan --no-system --no-wsl  # AWS only
```

| Flag | Description |
| --- | --- |
| `--aws` | Enable AWS scanning |
| `--aws-region` | Region to scan. Repeatable. Default `us-east-1` |
| `--aws-access-key`, `--aws-secret-key`, `--aws-session-token` | Explicit credentials. Default: AWS CLI credentials or IAM role |
| `--no-aws-ec2`, `--no-aws-lambda`, `--no-aws-ecs`, `--no-aws-sagemaker` | Skip a service |

AWS rows appear in the CSV with `source` set to `aws-ec2`, `aws-lambda`, `aws-ecs` or
`aws-sagemaker`, plus `aws_region`, `aws_instance_id`, `aws_instance_type`,
`aws_function_name`, `aws_runtime`, `aws_cluster`, `aws_container_name`,
`aws_container_image` and `aws_notebook_name` columns.

Limitations:

- AWS environments are inventory-only. Their interpreters are not probed, so pip,
  certifi, uv and conda versions are not checked: `env_verdict` is `not_probed` and
  the rows are not counted as compliant, action required or probe errors.
- Only Lambda reports a Python version (from the runtime identifier).
- A service that fails (for example `AccessDenied`) prints a warning and is skipped.
