# PyInstaller spec for the standalone PolyCopy server.
# Build with:  scripts/build_macos_app.sh   (or: pyinstaller packaging/polycopy.spec)
from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

datas, binaries, hiddenimports = [], [], []
for package in ("polymarket", "eth_account", "eth_keys", "eth_abi", "eth_utils", "eth_hash",
                "uvicorn", "websockets", "keyring"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("polycopy")
hiddenimports += collect_submodules("keyring.backends")
for dist in ("keyring", "polymarket-client", "eth-account", "eth-hash", "fastapi", "starlette",
             "pydantic"):
    datas += copy_metadata(dist)
datas += [("../polycopy/web/static", "polycopy/web/static")]

a = Analysis(
    ["launcher.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pandas", "polars", "pyarrow", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PolyCopy",
    console=True,
    target_arch=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="PolyCopy")
