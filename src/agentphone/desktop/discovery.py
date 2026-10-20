"""Bounded local discovery: processes, PATH, known locations and registry metadata."""

import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil


@dataclass
class AgentInstallation:
    id: str
    name: str
    command: str = ""
    application: str = ""
    running: bool = False
    supported: bool = True
    evidence: str = ""

    @property
    def callable(self):
        return bool(self.command and Path(self.command).is_file() and self.supported)

    def to_dict(self):
        return {**asdict(self), "callable": self.callable}


def first_file(candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    return ""


def desktop_client(path):
    """Electron chat clients with similar names are not vendor coding CLIs."""
    path = Path(path)
    return path.suffix.lower() == ".exe" and (
        (path.parent / "resources/app.asar").is_file()
        or path.parent.name.lower().endswith("-desktop")
    )


def process_paths():
    result = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            name = (process.info["name"] or "").lower()
            if any(
                key in name
                for key in [
                    "codex",
                    "deepseek",
                    "ollama",
                    "claude",
                    "lm studio",
                    "gemini",
                    "qwen",
                    "kimi",
                    "workbuddy",
                ]
            ):
                if process.info["exe"]:
                    result.append((name, Path(process.info["exe"])))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return result


def registry_locations():
    """Read only program names/locations; never credentials or uninstall commands."""
    if os.name != "nt":
        return []
    import winreg

    result = []
    root_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
        for view in [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]:
            try:
                with winreg.OpenKey(hive, root_path, 0, winreg.KEY_READ | view) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0]).lower()
                                if not any(
                                    word in name
                                    for word in [
                                        "deepseek",
                                        "codex",
                                        "ollama",
                                        "claude",
                                        "gemini",
                                        "qwen",
                                        "kimi",
                                        "workbuddy",
                                    ]
                                ):
                                    continue
                                for field in ["InstallLocation", "DisplayIcon"]:
                                    try:
                                        value = str(winreg.QueryValueEx(key, field)[0])
                                        if field == "DisplayIcon" and "," in value:
                                            path, suffix = value.rsplit(",", 1)
                                            if suffix.strip().lstrip("-").isdigit():
                                                value = path
                                        result.append((name, Path(value.strip('"'))))
                                    except OSError:
                                        pass
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def discover_agents(overrides=None, cached=None):
    overrides = overrides or {}
    cached = cached or {}
    home = Path.home()
    local = Path(os.getenv("LOCALAPPDATA", home / "AppData/Local"))
    program_files = Path(os.getenv("ProgramFiles", r"C:\Program Files"))
    processes = process_paths()
    locations = registry_locations()
    codex = AgentInstallation("codex", "Codex")
    codex_candidates = [overrides.get("codex")]
    versions = list((local / "OpenAI/Codex/bin").glob("*/codex.exe"))
    versions.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    codex_candidates.extend(versions)
    codex_candidates.append(cached.get("codex"))
    codex_candidates.append(shutil.which("codex"))
    for name, path in processes:
        if name == "codex.exe":
            codex.running = True
            if "bin" in [part.lower() for part in path.parts]:
                codex_candidates.append(path)
            else:
                codex.application = str(path)
    codex.command = first_file(codex_candidates)
    codex.evidence = "PATH / 本机 OpenAI 安装目录 / 运行进程"

    deepseek = AgentInstallation("deepseek_harness", "DeepSeek Harness")
    app_candidates = [
        local / "Programs/DeepSeek Harness/DeepSeek Harness.exe",
        local / "DeepSeek Harness/DeepSeek Harness.exe",
        program_files / "DeepSeek Harness/DeepSeek Harness.exe",
    ]
    for name, path in processes:
        if name == "deepseek harness.exe":
            app_candidates.insert(0, path)
            deepseek.running = True
    for name, path in locations:
        if "deepseek" in name and "harness" in name:
            app_candidates.append(
                path if path.suffix.lower() == ".exe" else path / "DeepSeek Harness.exe"
            )
    deepseek.application = first_file(app_candidates)
    dsh_candidates = [
        overrides.get("deepseek_harness"),
        shutil.which("dsh"),
        cached.get("deepseek_harness"),
    ]
    for application in app_candidates:
        dsh_candidates.append(application.parent / "resources/runtime/cli/bin/dsh.cmd")
    deepseek.command = first_file(dsh_candidates)
    if deepseek.command and not deepseek.application:
        command = Path(deepseek.command)
        if command.name.lower() == "dsh.cmd" and len(command.parents) >= 5:
            deepseek.application = first_file([command.parents[4] / "DeepSeek Harness.exe"])
    deepseek.evidence = "运行进程 / 常见安装目录 / 注册表安装位置 / PATH"

    ollama = AgentInstallation("ollama", "Ollama 本地模型")
    ollama.command = first_file(
        [
            overrides.get("ollama"),
            shutil.which("ollama"),
            local / "Programs/Ollama/ollama.exe",
            cached.get("ollama"),
        ]
    )
    ollama.application = first_file([local / "Programs/Ollama/ollama app.exe"])
    ollama.running = any("ollama" in name for name, _ in processes)
    ollama.evidence = "PATH / 常见安装目录 / 运行进程"
    workbuddy = AgentInstallation("workbuddy", "WorkBuddy")
    wb_candidates = [
        overrides.get("workbuddy"),
        local / "Programs/WorkBuddy/WorkBuddy.exe",
        program_files / "WorkBuddy/WorkBuddy.exe",
        cached.get("workbuddy"),
    ]
    # WorkBuddyAI is a separate application; never silently substitute it.
    for name, path in processes:
        if name == "workbuddy.exe":
            wb_candidates.insert(0, path)
            workbuddy.running = True
    for name, path in locations:
        if "workbuddy" in name and "workbuddyai" not in name.replace(" ", ""):
            wb_candidates.append(path if path.suffix.lower() == ".exe" else path / "WorkBuddy.exe")
    workbuddy.application = first_file(wb_candidates)
    workbuddy.command = workbuddy.application
    workbuddy.evidence = "腾讯原桌面程序 / 运行进程 / 安装目录 / 注册表；原生扩展适配"
    agents = [deepseek, codex, workbuddy, ollama]
    for agent_id, name in CLI_AGENTS:
        filenames = [agent_id + suffix for suffix in (".exe", ".cmd", ".bat", "")]
        candidates = [overrides.get(agent_id), cached.get(agent_id), shutil.which(agent_id)]
        directories = [
            home / ".local/bin",
            home / ".bun/bin",
            home / ".npm-global/bin",
            home / "AppData/Roaming/npm",
            local / "Microsoft/WinGet/Links",
            home / "AppData/Roaming/Python/Scripts",
        ]
        directories.extend((home / "AppData/Roaming/Python").glob("Python*/Scripts"))
        for directory in directories:
            candidates.extend(directory / filename for filename in filenames)
        running = False
        for _process_name, path in processes:
            if path.stem.lower() == agent_id:
                running = True
                candidates.append(path)
        for program_name, location in locations:
            if agent_id in program_name:
                candidates.extend([location, *(location / filename for filename in filenames)])
        application = first_file(
            candidate for candidate in candidates if candidate and desktop_client(candidate)
        )
        command = first_file(
            candidate for candidate in candidates if candidate and not desktop_client(candidate)
        )
        agents.append(
            AgentInstallation(
                agent_id,
                name,
                command,
                application=application,
                running=running,
                evidence="手动路径 / 路径缓存 / PATH / 用户安装目录 / 运行进程 / 注册表",
            )
        )
    return agents


CLI_AGENTS = [
    ("claude", "Claude Code"),
    ("gemini", "Gemini CLI"),
    ("qwen", "Qwen Code"),
    ("kimi", "Kimi CLI"),
]
