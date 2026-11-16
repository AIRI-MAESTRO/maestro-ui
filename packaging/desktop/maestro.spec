# Build on the target OS; the official DSH runtime is prepared by the workflow.
import json
from pathlib import Path
import sys
import tomllib

from PyInstaller.utils.hooks import collect_all, copy_metadata

root = Path(SPECPATH).parents[1]
version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
stage = root / 'build' / 'desktop-resources'
manifest = json.loads((stage / 'desktop-runtime.json').read_text())
datas = [(str(stage / 'desktop-runtime.json'), '.')]
binaries = []
hiddenimports = []
for package in ('care', 'mmar_mage', 'mmar_carl', 'gigaevo_client', 'webview', 'mcp'):
    data, binary, hidden = collect_all(package)
    datas += data
    binaries += binary
    hiddenimports += hidden
datas += copy_metadata('maestro-care', recursive=True)
datas += [(str(root / 'care' / 'web_assets'), 'web_assets')]
for integration in (root / 'integrations').glob('deepseek-harness*'):
    datas += [(str(integration), integration.name)]
for filename in manifest['files']:
    datas += [(str(stage / 'deepseek-harness-runtime' / filename), 'deepseek-harness-runtime')]
if sys.platform == 'darwin':
    data, binary, hidden = collect_all('LocalAuthentication')
    datas += data
    binaries += binary
    hiddenimports += hidden
a = Analysis([str(root / 'packaging' / 'desktop' / 'entry.py')],
             pathex=[str(root)], datas=datas, binaries=binaries,
             hiddenimports=hiddenimports, excludes=['tkinter'])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Maestro',
          console=sys.platform not in ('darwin', 'win32'), strip=False, upx=False)
helper = EXE(pyz, a.scripts, [], exclude_binaries=True, name='maestro-python',
             console=True, strip=False, upx=False)
coll = COLLECT(exe, helper, a.binaries, a.datas, name='Maestro', strip=False, upx=False)
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='Maestro.app', icon=str(root / 'packaging/macos/Maestro.icns'),
                 bundle_identifier='org.airi.maestro', info_plist={
                     'CFBundleShortVersionString': version,
                     'CFBundleVersion': version,
                     'NSHighResolutionCapable': True,
                     'NSFaceIDUsageDescription': 'MAESTRO uses device biometrics only when you enable App Lock.',
                     'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
                 })
