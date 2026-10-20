# Build with: python -m PyInstaller --distpath OUTPUT --workpath WORK scripts/AgentPhone.spec
from pathlib import Path
from importlib import metadata
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

project = Path(SPECPATH).parent
datas, binaries, hiddenimports = [], [], collect_submodules('uvicorn')
datas.append((str(project / 'src/agentphone/desktop/native/dsh-window.exe'), 'agentphone/desktop/native'))
datas.append((str(project / 'src/agentphone/desktop/native/workbuddy-bridge'), 'agentphone/desktop/native/workbuddy-bridge'))
# Desktop package resources resolved through importlib.resources.
for resource in ['agentphone.png', 'agentphone.ico', 'agentphone.svg', 'guide.html']:
    datas.append((str(project / 'src/agentphone/desktop' / resource), 'agentphone/desktop'))
web_dir = project / 'src/agentphone/web'
if web_dir.is_dir():
    datas.append((str(web_dir), 'agentphone/web'))
for package in ['agentphone', 'mcp', 'mcp_types', 'httpx2', 'httpcore2']:
    collected = collect_all(package, filter_submodules=lambda name: not name.startswith('mcp.cli'))
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

# Include redistribution notices for bundled installed distributions.
for distribution in metadata.distributions():
    name = distribution.metadata.get('Name', 'unknown')
    for item in distribution.files or []:
        if any(word in item.name.lower() for word in ('license', 'copying', 'notice')):
            source = Path(distribution.locate_file(item))
            if source.is_file():
                datas.append((str(source), 'licenses/' + name))
for name in ['LICENSE', 'LICENSE.txt']:
    license_path = Path(sys.base_prefix) / name
    if license_path.is_file():
        datas.append((str(license_path), 'licenses/Python'))

analysis = Analysis(
    [str(project / 'scripts/desktop_entry.py')],
    pathex=[str(project / 'src')], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['mcp.cli'], noarchive=False, optimize=0,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [], name='AgentPhone',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    runtime_tmpdir=None, console=False, disable_windowed_traceback=False,
    icon=str(project / 'src/agentphone/desktop/agentphone.ico'),
)
