"""Check pip, certifi, uv and conda versions across every Python environment found.

Scans the whole system (all fixed drives on Windows, the filesystem root on Linux)
for conda installations and environments, uv-managed Pythons, virtual environments
and system Pythons, probes each interpreter, compares the installed versions
against the required minimums, and writes a CSV plus a report-back email body.

Runs natively on Windows and Linux, and will also probe WSL distros when run from
Windows. Containers, CI/CD and deployed environments are out of scope and must be
checked separately.
"""

import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Deliberately stdlib-only: importing geo_pipeline would pull in geopandas.
ROOT_DIR = Path(__file__).resolve().parents[1]

IS_WINDOWS = os.name == "nt"

REPORT_EMAIL = "datascience@ukkhsa.gov.uk"

REQUIRED_VERSIONS = {
    "pip": "26.2.2",
    "certifi": "2026.10.19",
    "uv": "0.13.6",
    "conda": "26.9.4",
}

CHECKED_PACKAGES = ("pip", "certifi", "uv", "conda")

# Packages that are not expected in every environment, so absence is not a failure.
OPTIONAL_PACKAGES = {"certifi", "uv", "conda"}

CSV_FIELDNAMES = [
    "source",
    "name",
    "env_path",
    "python_executable",
    "python_version",
    "implementation",
    "pip_version",
    "pip_status",
    "certifi_version",
    "certifi_path",
    "certifi_status",
    "uv_version",
    "uv_status",
    "conda_version",
    "conda_status",
    "env_verdict",
    "conda_tool_version",
    "uv_tool_version",
    "status",
    "error",
]

# Directories that never contain a usable environment root but are expensive to walk.
PRUNE_DIR_NAMES = {
    "$Recycle.Bin",
    ".cache",
    ".cargo",
    ".git",
    ".hg",
    ".npm",
    ".svn",
    ".Trash",
    "__pycache__",
    "DriverStore",
    "Installer",
    "lost+found",
    "node_modules",
    "proc",
    "site-packages",
    "snap",
    "sys",
    "SystemApps",
    "Temp",
    "WinSxS",
    "Windows",
}

# Pseudo-filesystems and mount points that must never be walked.
PRUNE_ABSOLUTE_PATHS = {
    "/proc",
    "/sys",
    "/dev",
    "/run",
    "/snap",
    "/var/lib/docker",
}

# Directory names used by the common conda distributions.
CONDA_DIR_NAMES = ("miniforge3", "mambaforge", "anaconda3", "miniconda3", "conda")

# Extra roots worth scanning explicitly on POSIX, where envs live outside $HOME.
POSIX_HINT_ROOTS = (
    "/opt",
    "/srv",
    "/usr/local",
    "/home",
    "/var/www",
)

# Package names arrive as argv[1] rather than being interpolated into this source.
PROBE_SNIPPET = (
    "import json,platform,sys\n"
    "packages = {}\n"
    "for name in json.loads(sys.argv[1]):\n"
    "    try:\n"
    "        from importlib.metadata import version, distribution\n"
    "        packages[name] = {'version': version(name),"
    " 'path': str(distribution(name).locate_file(name))}\n"
    "    except Exception:\n"
    "        packages[name] = {'version': None, 'path': None}\n"
    "print(json.dumps({'python_version': platform.python_version(),"
    " 'implementation': platform.python_implementation(),"
    " 'executable': sys.executable, 'prefix': sys.prefix,"
    " 'packages': packages}))\n"
)


def parse_version(value: Optional[str]) -> Tuple[int, ...]:
    """Convert a version string to a tuple of integers for comparison.

    Trailing non-numeric suffixes such as rc1, b2, .post0 or +local are discarded.

    Args:
        value: Version string such as "26.2.2" or "2026.10.19".

    Returns:
        Tuple of integer components, or an empty tuple if nothing could be parsed.
    """
    if not value:
        return ()

    components = []
    for part in str(value).split("."):
        digits = ""
        for char in part.strip():
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        components.append(int(digits))
    return tuple(components)


def meets_minimum(actual: Optional[str], minimum: str, optional: bool = False) -> str:
    """Compare an installed version against a required minimum.

    Args:
        actual: Installed version string, or None if the package is absent.
        minimum: Required minimum version string.
        optional: When True, an absent package reports "n/a" instead of "missing".

    Returns:
        One of "ok", "outdated", "missing", "n/a" or "unknown".
    """
    if not actual:
        return "n/a" if optional else "missing"

    actual_parts = parse_version(actual)
    minimum_parts = parse_version(minimum)
    if not actual_parts or not minimum_parts:
        return "unknown"

    width = max(len(actual_parts), len(minimum_parts))
    actual_parts += (0,) * (width - len(actual_parts))
    minimum_parts += (0,) * (width - len(minimum_parts))
    return "ok" if actual_parts >= minimum_parts else "outdated"


def run_command(command: List[str], timeout: int = 60) -> Optional[str]:
    """Run a command and return its stdout, or None if it fails or is unavailable.

    Args:
        command: Argument list to execute. Never run through a shell.
        timeout: Seconds to wait before giving up.

    Returns:
        Captured stdout as text, or None on any failure.
    """
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def get_tool_version(tool: str) -> Optional[str]:
    """Return the version string reported by `<tool> --version`, or None."""
    executable = shutil.which(tool)
    if not executable:
        return None
    output = run_command([executable, "--version"])
    if not output:
        return None
    return output.strip().splitlines()[0].strip()


def interpreter_for_env(env_path: Path) -> Path:
    """Return the interpreter path inside an environment directory.

    Conda roots put the interpreter at the top level while venvs put it under
    Scripts/ (Windows) or bin/ (POSIX), so both layouts are tried.

    Args:
        env_path: Environment prefix directory.

    Returns:
        The first candidate that exists, else the conventional path for the platform.
    """
    if IS_WINDOWS:
        candidates = [env_path / "python.exe", env_path / "Scripts" / "python.exe"]
    else:
        candidates = [
            env_path / "bin" / "python",
            env_path / "bin" / "python3",
            env_path / "python",
        ]

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return candidates[0]


def fixed_drive_roots() -> List[Path]:
    """Return the root of every fixed drive on Windows."""
    import ctypes

    roots = []
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        bitmask = kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return [Path("C:/")]

    drive_fixed = 3
    for index in range(26):
        if not bitmask & (1 << index):
            continue
        letter = f"{chr(ord('A') + index)}:\\"
        try:
            if kernel32.GetDriveTypeW(letter) == drive_fixed:
                roots.append(Path(letter))
        except OSError:
            continue
    return roots or [Path("C:/")]


def default_scan_roots() -> List[Path]:
    """Return the roots to scan for virtual environments, shallowest first."""
    roots = [Path.home(), ROOT_DIR]
    if IS_WINDOWS:
        roots.extend(fixed_drive_roots())
    else:
        roots.extend(Path(p) for p in POSIX_HINT_ROOTS)
        roots.append(Path("/"))
    return roots


def find_conda_installations() -> List[Dict[str, str]]:
    """Discover every conda installation root on the machine.

    Looks at `conda info --json`, the CONDA_EXE/CONDA_PREFIX environment
    variables, and the well-known distribution directory names.

    Returns:
        One entry per installation root, tagged with source "conda-root".
    """
    candidates: List[Path] = []

    conda = shutil.which("conda") or os.getenv("CONDA_EXE")
    if conda:
        output = run_command([conda, "info", "--json"])
        if output:
            try:
                info = json.loads(output)
                if info.get("root_prefix"):
                    candidates.append(Path(info["root_prefix"]))
                for envs_dir in info.get("envs_dirs", []):
                    # envs_dirs entries are "<root>/envs"; the parent is the root.
                    candidates.append(Path(envs_dir).parent)
            except json.JSONDecodeError:
                pass
        candidates.append(Path(conda).resolve().parent.parent)

    prefix = os.getenv("CONDA_PREFIX")
    if prefix:
        candidates.append(Path(prefix))

    search_bases = [Path.home()]
    if IS_WINDOWS:
        search_bases.extend([Path("C:/ProgramData"), Path("C:/")])
    else:
        search_bases.extend([Path("/opt"), Path("/usr/local"), Path("/srv")])

    for base in search_bases:
        for name in CONDA_DIR_NAMES:
            candidates.append(base / name)

    installations = []
    seen = set()
    for candidate in candidates:
        try:
            root = candidate.resolve()
        except OSError:
            continue
        key = str(root).lower()
        if key in seen or not root.is_dir():
            continue
        if not interpreter_for_env(root).is_file():
            continue
        seen.add(key)
        installations.append(
            {
                "source": "conda-root",
                "name": root.name,
                "env_path": str(root),
                "python_executable": str(interpreter_for_env(root)),
            }
        )
    return installations


def conda_executables(installations: List[Dict[str, str]]) -> List[str]:
    """Return every conda executable worth querying for environments."""
    executables = []
    on_path = shutil.which("conda") or os.getenv("CONDA_EXE")
    if on_path:
        executables.append(on_path)

    sub_dir = "Scripts" if IS_WINDOWS else "bin"
    suffix = ".exe" if IS_WINDOWS else ""
    for installation in installations:
        candidate = Path(installation["env_path"]) / sub_dir / f"conda{suffix}"
        if candidate.is_file():
            executables.append(str(candidate))

    unique = []
    seen = set()
    for executable in executables:
        key = executable.lower()
        if key not in seen:
            seen.add(key)
            unique.append(executable)
    return unique


def find_conda_envs(installations: Optional[List[Dict[str, str]]] = None) -> List[Dict[str, str]]:
    """Discover conda environments across every conda installation."""
    executables = conda_executables(installations or [])
    if not executables:
        return []

    environments = []
    for conda in executables:
        output = run_command([conda, "env", "list", "--json"])
        if not output:
            continue
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            continue

        for env in payload.get("envs", []):
            env_path = Path(env)
            python_exe = interpreter_for_env(env_path)
            environments.append(
                {
                    "source": "conda",
                    "name": env_path.name,
                    "env_path": str(env_path),
                    "python_executable": str(python_exe),
                }
            )
    return environments


def find_uv_pythons() -> List[Dict[str, str]]:
    """Discover installed uv-managed Python interpreters."""
    uv = shutil.which("uv")
    if not uv:
        return []

    environments = []
    output = run_command([uv, "python", "list", "--output-format", "json"])
    if output:
        try:
            for entry in json.loads(output):
                path = entry.get("path")
                if not path:
                    continue
                environments.append(
                    {
                        "source": "uv",
                        "name": entry.get("key") or entry.get("version") or Path(path).stem,
                        "env_path": str(Path(path).parent),
                        "python_executable": path,
                    }
                )
            return environments
        except json.JSONDecodeError:
            pass

    # Older uv builds have no JSON output: fall back to "<key>    <path>" text lines.
    output = run_command([uv, "python", "list"])
    if not output:
        return []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        candidate = Path(parts[-1])
        if not candidate.is_absolute() or not candidate.exists():
            continue
        environments.append(
            {
                "source": "uv",
                "name": parts[0],
                "env_path": str(candidate.parent),
                "python_executable": str(candidate),
            }
        )
    return environments


def find_venvs(
    scan_roots: List[Path], max_depth: int, scan_timeout: int = 300
) -> List[Dict[str, str]]:
    """Walk the given roots looking for virtual environments (pyvenv.cfg).

    Args:
        scan_roots: Directories to walk, shallowest/most likely first.
        max_depth: Maximum depth below each root to descend.
        scan_timeout: Seconds after which the walk stops and returns what it has.

    Returns:
        One entry per virtual environment found.
    """
    environments = []
    seen_roots = set()
    visited_dirs = set()
    deadline = time.monotonic() + scan_timeout if scan_timeout > 0 else None
    timed_out = False

    for root in scan_roots:
        if timed_out:
            break
        root = root.expanduser()
        if not root.is_dir():
            continue
        try:
            resolved_root = str(root.resolve()).lower()
        except OSError:
            continue
        if resolved_root in seen_roots:
            continue
        seen_roots.add(resolved_root)
        root_depth = len(root.parts)

        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
            if deadline and time.monotonic() > deadline:
                print(f"Scan timeout reached after {scan_timeout}s; continuing with partial results.")
                timed_out = True
                break

            current = Path(dirpath)
            # Guard against symlink and junction loops revisiting the same directory.
            try:
                real = str(current.resolve()).lower()
            except OSError:
                dirnames[:] = []
                continue
            if real in visited_dirs:
                dirnames[:] = []
                continue
            visited_dirs.add(real)

            if len(current.parts) - root_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [
                d
                for d in dirnames
                if d not in PRUNE_DIR_NAMES
                and not d.startswith("$")
                and f"{dirpath.rstrip('/')}/{d}" not in PRUNE_ABSOLUTE_PATHS
            ]

            if "pyvenv.cfg" in filenames:
                dirnames[:] = []
                environments.append(
                    {
                        "source": "venv",
                        "name": current.name,
                        "env_path": str(current),
                        "python_executable": str(interpreter_for_env(current)),
                    }
                )
    return environments


def find_system_pythons() -> List[Dict[str, str]]:
    """Discover system Pythons via platform-specific locations and PATH lookups."""
    environments = []

    if IS_WINDOWS:
        output = run_command(["py", "-0p"])
        for line in (output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            # Lines look like: "-V:3.12 *        C:\Python312\python.exe"
            for token in line.split():
                candidate = Path(token)
                if candidate.is_absolute() and candidate.exists() and candidate.suffix.lower() == ".exe":
                    environments.append(
                        {
                            "source": "system",
                            "name": line.split()[0],
                            "env_path": str(candidate.parent),
                            "python_executable": str(candidate),
                        }
                    )
                    break
    else:
        patterns = [
            (Path("/usr/bin"), "python3*"),
            (Path("/usr/bin"), "python"),
            (Path("/usr/local/bin"), "python3*"),
            (Path("/usr/local/bin"), "python"),
            (Path("/bin"), "python3*"),
            (Path.home() / ".pyenv/versions", "*/bin/python"),
        ]
        for directory, pattern in patterns:
            if not directory.is_dir():
                continue
            try:
                matches = sorted(directory.glob(pattern))
            except OSError:
                continue
            for candidate in matches:
                # Skip config helpers such as python3.12-config.
                if not candidate.is_file() or "-config" in candidate.name:
                    continue
                environments.append(
                    {
                        "source": "system",
                        "name": candidate.name,
                        "env_path": str(candidate.parent),
                        "python_executable": str(candidate),
                    }
                )

        for opt_bin in sorted(Path("/opt").glob("*/bin/python*")) if Path("/opt").is_dir() else []:
            if opt_bin.is_file() and "-config" not in opt_bin.name:
                environments.append(
                    {
                        "source": "system",
                        "name": opt_bin.name,
                        "env_path": str(opt_bin.parent),
                        "python_executable": str(opt_bin),
                    }
                )

    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if not found or Path(found).suffix.lower() in {".bat", ".cmd"}:
            continue
        candidate = Path(found)
        environments.append(
            {
                "source": "system",
                "name": candidate.stem,
                "env_path": str(candidate.parent),
                "python_executable": str(candidate),
            }
        )

    environments.append(
        {
            "source": "system",
            "name": "current-interpreter",
            "env_path": sys.prefix,
            "python_executable": sys.executable,
        }
    )
    return environments


def wsl_distros() -> List[str]:
    """Return the names of the installed WSL distributions."""
    if not IS_WINDOWS or not shutil.which("wsl.exe"):
        return []

    try:
        completed = subprocess.run(
            ["wsl.exe", "-l", "-q"],
            capture_output=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []

    # wsl.exe emits UTF-16LE on most builds, with a UTF-8 fallback on newer ones.
    for encoding in ("utf-16-le", "utf-8"):
        try:
            text = completed.stdout.decode(encoding)
        except UnicodeDecodeError:
            continue
        names = [line.strip().strip("\x00") for line in text.splitlines()]
        distros = [n for n in names if n and not n.startswith("docker-desktop")]
        if distros:
            return distros
    return []


def find_wsl_environments() -> List[Dict[str, str]]:
    """Discover Python interpreters and conda envs inside each WSL distribution."""
    environments = []

    for distro in wsl_distros():
        prefix = ["wsl.exe", "-d", distro, "--"]
        output = run_command(
            prefix + ["bash", "-lc", "command -v python3 python 2>/dev/null"],
            timeout=60,
        )
        for line in (output or "").splitlines():
            path = line.strip()
            if not path.startswith("/"):
                continue
            environments.append(
                {
                    "source": f"wsl:{distro}",
                    "name": f"{distro}:{Path(path).name}",
                    "env_path": str(Path(path).parent),
                    "python_executable": path,
                    "command_prefix": json.dumps(prefix),
                }
            )

        conda_output = run_command(
            prefix + ["bash", "-lc", "conda env list --json 2>/dev/null"],
            timeout=90,
        )
        if conda_output:
            try:
                payload = json.loads(conda_output)
            except json.JSONDecodeError:
                payload = {}
            for env in payload.get("envs", []):
                env_path = Path(env)
                environments.append(
                    {
                        "source": f"wsl:{distro}",
                        "name": f"{distro}:{env_path.name}",
                        "env_path": str(env_path),
                        "python_executable": f"{env}/bin/python",
                        "command_prefix": json.dumps(prefix),
                    }
                )
    return environments


def probe_interpreter(
    python_exe: str, timeout: int, command_prefix: Optional[List[str]] = None
) -> Dict[str, Optional[str]]:
    """Run an interpreter to read its Python version and checked package versions.

    Args:
        python_exe: Path to the interpreter to probe.
        timeout: Seconds to allow the probe subprocess to run.
        command_prefix: Optional launcher prefix, e.g. the wsl.exe invocation.

    Returns:
        Dict with python_version, implementation, pip_version, certifi_version,
        certifi_path, uv_version, conda_version, status and error keys. Failures
        are reported, never raised.
    """
    failure: Dict[str, Optional[str]] = {
        "python_version": None,
        "implementation": None,
        "pip_version": None,
        "certifi_version": None,
        "certifi_path": None,
        "uv_version": None,
        "conda_version": None,
    }

    # A prefixed interpreter lives inside WSL, so it is not visible to Path here.
    if not command_prefix and not Path(python_exe).is_file():
        return {**failure, "status": "error", "error": "interpreter not found"}

    command = list(command_prefix or []) + [
        python_exe,
        "-c",
        PROBE_SNIPPET,
        json.dumps(list(CHECKED_PACKAGES)),
    ]

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {**failure, "status": "error", "error": f"probe timed out after {timeout}s"}
    except OSError as exc:
        return {**failure, "status": "error", "error": str(exc)}

    if completed.returncode != 0:
        message = (completed.stderr or "").strip().splitlines()
        return {
            **failure,
            "status": "error",
            "error": message[-1] if message else f"exit code {completed.returncode}",
        }

    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {**failure, "status": "error", "error": "unreadable probe output"}

    packages = payload.get("packages") or {}
    return {
        "python_version": payload.get("python_version"),
        "implementation": payload.get("implementation"),
        "pip_version": packages.get("pip", {}).get("version"),
        "certifi_version": packages.get("certifi", {}).get("version"),
        "certifi_path": packages.get("certifi", {}).get("path"),
        "uv_version": packages.get("uv", {}).get("version"),
        "conda_version": packages.get("conda", {}).get("version"),
        "status": "ok",
        "error": None,
    }


def evaluate_compliance(
    row: Dict[str, object], minimums: Dict[str, str]
) -> Dict[str, object]:
    """Compare a probed environment's package versions against the minimums.

    Args:
        row: Probed environment row containing the *_version keys.
        minimums: Mapping of package name to required minimum version.

    Returns:
        Dict with pip_status, certifi_status, uv_status, conda_status and
        env_verdict keys.
    """
    if row.get("status") == "error":
        return {
            **{f"{name}_status": "unknown" for name in CHECKED_PACKAGES},
            "env_verdict": "probe_error",
        }

    statuses = {
        f"{name}_status": meets_minimum(
            str(row.get(f"{name}_version") or "") or None,
            minimums[name],
            optional=name in OPTIONAL_PACKAGES,
        )
        for name in CHECKED_PACKAGES
    }
    failed = {"outdated", "missing"}
    verdict = "action_required" if failed & set(statuses.values()) else "compliant"
    return {**statuses, "env_verdict": verdict}


def discover_environments(args: argparse.Namespace) -> List[Dict[str, str]]:
    """Run the enabled discovery backends and deduplicate by interpreter path."""
    discovered: List[Dict[str, str]] = []
    installations: List[Dict[str, str]] = []

    if not args.no_conda:
        print("Discovering conda installations...")
        installations = find_conda_installations()
        print(f"Found {len(installations)} conda installation(s).")
        discovered.extend(installations)
        print("Discovering conda environments...")
        discovered.extend(find_conda_envs(installations))
    if not args.no_uv:
        print("Discovering uv-managed Pythons...")
        discovered.extend(find_uv_pythons())
    if not args.no_venv_scan:
        roots = default_scan_roots() + [Path(r) for r in (args.scan_root or [])]
        print(
            f"Scanning {len(roots)} root(s) for virtual environments "
            f"(depth {args.max_depth}, timeout {args.scan_timeout}s)..."
        )
        discovered.extend(find_venvs(roots, args.max_depth, args.scan_timeout))
    if not args.no_system:
        print("Discovering system Pythons...")
        discovered.extend(find_system_pythons())
    if not args.no_wsl and IS_WINDOWS:
        print("Discovering WSL environments...")
        discovered.extend(find_wsl_environments())

    unique: Dict[str, Dict[str, str]] = {}
    for env in discovered:
        if env.get("command_prefix"):
            # WSL paths are not resolvable locally, so key them by distro + path.
            key = f"{env['source']}|{env['python_executable']}".lower()
        else:
            try:
                key = str(Path(env["python_executable"]).resolve()).lower()
            except OSError:
                key = env["python_executable"].lower()
        existing = unique.get(key)
        if existing is None:
            unique[key] = env
        elif env["source"] not in existing["source"].split("+"):
            existing["source"] = f"{existing['source']}+{env['source']}"
    return list(unique.values())


def collect_environments(
    args: argparse.Namespace, minimums: Dict[str, str]
) -> List[Dict[str, object]]:
    """Discover and probe every environment, returning fully populated CSV rows."""
    environments = discover_environments(args)
    conda_version = get_tool_version("conda")
    uv_version = get_tool_version("uv")

    total = len(environments)
    print(f"Probing {total} environment(s) with {args.max_workers} worker(s)...")

    def probe_one(env: Dict[str, str]) -> Dict[str, object]:
        prefix = json.loads(env["command_prefix"]) if env.get("command_prefix") else None
        probe = probe_interpreter(env["python_executable"], args.timeout, prefix)
        row: Dict[str, object] = {
            **env,
            **probe,
            "conda_tool_version": conda_version,
            "uv_tool_version": uv_version,
        }
        row.update(evaluate_compliance(row, minimums))
        return row

    rows: List[Dict[str, object]] = []
    if args.max_workers <= 1:
        for index, env in enumerate(environments, start=1):
            print(f"[{index}/{total}] Probing {env['python_executable']}")
            rows.append(probe_one(env))
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_workers) as pool:
            futures = [pool.submit(probe_one, env) for env in environments]
            for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                rows.append(future.result())
                print(f"[{index}/{total}] probed", end="\r", flush=True)
        print()

    rows.sort(key=lambda row: (str(row["source"]), str(row["name"]).lower()))
    return rows


def build_summary(
    rows: List[Dict[str, object]],
    minimums: Dict[str, str],
    uv_tool_version: Optional[str],
    conda_tool_version: Optional[str] = None,
    forced_status: str = "auto",
) -> Dict[str, object]:
    """Summarise compliance across all probed environments.

    Args:
        rows: Fully populated environment rows.
        minimums: Mapping of package name to required minimum version.
        uv_tool_version: Host-level uv version string, or None if uv is absent.
        conda_tool_version: Host-level conda version string, or None if absent.
        forced_status: Override for the headline status, or "auto" to derive it.

    Returns:
        Dict with counts, the outstanding environment list, the host uv status
        and the headline overall_status.
    """
    compliant = [r for r in rows if r.get("env_verdict") == "compliant"]
    action_required = [r for r in rows if r.get("env_verdict") == "action_required"]
    probe_errors = [r for r in rows if r.get("env_verdict") == "probe_error"]

    outstanding = []
    for row in action_required:
        failures = [
            f"{name} {row.get(f'{name}_version') or 'not installed'} "
            f"({row.get(f'{name}_status')}, requires >= {minimums[name]})"
            for name in CHECKED_PACKAGES
            if row.get(f"{name}_status") in {"outdated", "missing"}
        ]
        outstanding.append(
            {
                "name": row.get("name"),
                "source": row.get("source"),
                "python_executable": row.get("python_executable"),
                "failures": failures,
            }
        )

    uv_tool_status = "n/a"
    if uv_tool_version:
        # "uv --version" prints e.g. "uv 0.13.6 (abc1234 2026-01-01)".
        parts = uv_tool_version.split()
        uv_tool_status = meets_minimum(parts[1] if len(parts) > 1 else None, minimums["uv"])

    conda_tool_status = "n/a"
    if conda_tool_version:
        # "conda --version" prints e.g. "conda 26.9.4".
        parts = conda_tool_version.split()
        conda_tool_status = meets_minimum(
            parts[1] if len(parts) > 1 else None, minimums["conda"]
        )

    conda_installations = [
        {
            "name": r.get("name"),
            "env_path": r.get("env_path"),
            "conda_version": r.get("conda_version"),
            "conda_status": r.get("conda_status"),
        }
        for r in rows
        if "conda-root" in str(r.get("source", ""))
    ]

    if forced_status == "auto":
        overall_status = "Still outstanding" if outstanding else "No issue found"
    else:
        overall_status = {
            "no-issue": "No issue found",
            "updated": "Updated",
            "outstanding": "Still outstanding",
        }[forced_status]

    return {
        "total": len(rows),
        "compliant": len(compliant),
        "action_required": len(action_required),
        "probe_errors": len(probe_errors),
        "outstanding": outstanding,
        "uv_tool_version": uv_tool_version,
        "uv_tool_status": uv_tool_status,
        "conda_tool_version": conda_tool_version,
        "conda_tool_status": conda_tool_status,
        "conda_installations": conda_installations,
        "minimums": minimums,
        "overall_status": overall_status,
    }


def build_email_body(summary: Dict[str, object], csv_path: Path) -> str:
    """Build a plain-text report-back email body for the data science inbox."""
    minimums: Dict[str, str] = summary["minimums"]  # type: ignore[assignment]
    uv_tool = summary["uv_tool_version"] or "not installed"
    conda_tool = summary["conda_tool_version"] or "not installed"
    lines = [
        f"To: {REPORT_EMAIL}",
        "Subject: Python environment check - pip / certifi / uv / conda",
        "",
        f"Status: {summary['overall_status']}",
        f"Checked on: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Required versions:",
        f"  pip >= {minimums['pip']}",
        f"  certifi >= {minimums['certifi']}",
        f"  uv >= {minimums['uv']} (where used)",
        f"  conda >= {minimums['conda']} (where used)",
        "",
        "Environments checked (conda, uv-managed, virtual environments, system, WSL):",
        f"  Total environments: {summary['total']}",
        f"  Compliant: {summary['compliant']}",
        f"  Action required: {summary['action_required']}",
        f"  Could not be probed: {summary['probe_errors']}",
        f"  Host uv tool: {uv_tool} ({summary['uv_tool_status']})",
        f"  Host conda tool: {conda_tool} ({summary['conda_tool_status']})",
        "",
    ]

    if summary["conda_installations"]:
        lines.append("Conda installations:")
        for install in summary["conda_installations"]:
            lines.append(
                f"  - {install['env_path']}: conda "
                f"{install['conda_version'] or 'not found'} ({install['conda_status']})"
            )
        lines.append("")

    if summary["outstanding"]:
        lines.append("Environments still outstanding:")
        for env in summary["outstanding"]:
            lines.append(f"  - {env['name']} [{env['source']}] {env['python_executable']}")
            for failure in env["failures"]:
                lines.append(f"      {failure}")
        lines.extend(
            [
                "",
                "Blockers: <TO BE COMPLETED>",
                "Expected completion date: <TO BE COMPLETED>",
            ]
        )
    else:
        lines.append("No outstanding local environments.")

    lines.extend(
        [
            "",
            "Containers, CI/CD and deployed environments were checked separately:",
            "  <TO BE COMPLETED>",
            "",
            f"Full report: {csv_path}",
        ]
    )
    return "\n".join(lines) + "\n"


def write_csv(rows: List[Dict[str, object]], output_dir: str) -> Path:
    """Write the environment rows to a timestamped CSV and return its path."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = Path(output_dir) / f"env_inventory_{timestamp}.csv"

    with open(output_file, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return output_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check pip, certifi and uv versions across every Python environment "
            "detected on this machine and report compliance as CSV. Containers, "
            "CI/CD and deployed environments are out of scope."
        )
    )
    parser.add_argument(
        "--output-dir",
        default="data/reports",
        help="Directory for the CSV report. Default: data/reports",
    )
    parser.add_argument(
        "--scan-root",
        action="append",
        help="Extra directory to scan for virtual environments. Repeatable.",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=6,
        help="Maximum directory depth for the virtual environment scan. Default: 6",
    )
    parser.add_argument(
        "--scan-timeout",
        type=int,
        default=300,
        help="Seconds to spend scanning for virtual environments. Default: 300",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Interpreters to probe in parallel. 1 disables parallelism. Default: 8",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="Seconds allowed per interpreter probe. Default: 20",
    )
    parser.add_argument(
        "--min-pip",
        default=REQUIRED_VERSIONS["pip"],
        help=f"Minimum acceptable pip version. Default: {REQUIRED_VERSIONS['pip']}",
    )
    parser.add_argument(
        "--min-certifi",
        default=REQUIRED_VERSIONS["certifi"],
        help=f"Minimum acceptable certifi version. Default: {REQUIRED_VERSIONS['certifi']}",
    )
    parser.add_argument(
        "--min-uv",
        default=REQUIRED_VERSIONS["uv"],
        help=f"Minimum acceptable uv version. Default: {REQUIRED_VERSIONS['uv']}",
    )
    parser.add_argument(
        "--min-conda",
        default=REQUIRED_VERSIONS["conda"],
        help=f"Minimum acceptable conda version. Default: {REQUIRED_VERSIONS['conda']}",
    )
    parser.add_argument(
        "--status",
        choices=["auto", "no-issue", "updated", "outstanding"],
        default="auto",
        help=(
            "Headline status for the report-back email. Use 'updated' after you have "
            "remediated the environments yourself. Default: auto"
        ),
    )
    parser.add_argument(
        "--email-out",
        help=(
            "Path for the report-back email body. "
            "Default: <output-dir>/env_compliance_email_<timestamp>.txt"
        ),
    )
    parser.add_argument(
        "--no-email", action="store_true", help="Do not write the report-back email body."
    )
    parser.add_argument("--no-conda", action="store_true", help="Skip conda discovery.")
    parser.add_argument("--no-uv", action="store_true", help="Skip uv discovery.")
    parser.add_argument(
        "--no-venv-scan",
        action="store_true",
        help="Skip the on-disk virtual environment scan.",
    )
    parser.add_argument("--no-system", action="store_true", help="Skip system Python discovery.")
    parser.add_argument(
        "--no-wsl", action="store_true", help="Skip WSL distribution discovery (Windows only)."
    )
    parser.add_argument(
        "--print",
        dest="print_csv",
        action="store_true",
        help="Also echo the CSV content to stdout.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    minimums = {
        "pip": args.min_pip,
        "certifi": args.min_certifi,
        "uv": args.min_uv,
        "conda": args.min_conda,
    }

    rows = collect_environments(args, minimums)
    if not rows:
        print("No Python environments detected.")
        return

    output_file = write_csv(rows, args.output_dir)
    summary = build_summary(
        rows,
        minimums,
        str(rows[0].get("uv_tool_version") or "") or None,
        str(rows[0].get("conda_tool_version") or "") or None,
        args.status,
    )

    print()
    print(f"Overall status: {summary['overall_status']}")
    print(f"Environments: {summary['total']}")
    print(f"Compliant: {summary['compliant']}")
    print(f"Action required: {summary['action_required']}")
    print(f"Could not be probed: {summary['probe_errors']}")
    print(f"Host uv tool: {summary['uv_tool_version'] or 'not installed'} ({summary['uv_tool_status']})")
    print(
        f"Host conda tool: {summary['conda_tool_version'] or 'not installed'}"
        f" ({summary['conda_tool_status']})"
    )

    for env in summary["outstanding"]:
        print(f"  - {env['name']} [{env['source']}]: {'; '.join(env['failures'])}")

    print(f"CSV output: {output_file}")

    if not args.no_email:
        email_body = build_email_body(summary, output_file)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        email_path = Path(
            args.email_out or Path(args.output_dir) / f"env_compliance_email_{timestamp}.txt"
        )
        email_path.parent.mkdir(parents=True, exist_ok=True)
        email_path.write_text(email_body, encoding="utf-8")
        print(f"Email body: {email_path}")
        print()
        print(email_body)

    if args.print_csv:
        print()
        print(output_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
