# PyInstaller spec. One folder, two programs that share the Python runtime and every library:
#   找岗位助手   the window (macOS: a .app bundle that also holds `djs` in Contents/MacOS)
#   djs          console program for MCP clients and scripts
# Build: pyinstaller --noconfirm packaging/dear-job-seeker.spec
# Result in dist/: dear-job-seeker/ (Windows: the folder to ship) and, on macOS, 找岗位助手.app built from it (the app is
# what to ship there)
import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))  # bundle this source tree, whether or not (and however) it is installed

NAME = "找岗位助手"
FOLDER = "dear-job-seeker"  # ASCII on purpose: it is what gets zipped on Windows
BUNDLE_ID = "io.github.daijinma.dearjobseeker"
WINDOW, CONSOLE = "entry_window", "entry_console"  # entry scripts next to this spec
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
OPTIONS = [("X utf8", None, "OPTION")]  # Python text I/O is UTF-8 everywhere (Windows would use the ANSI code page)

analysis = Analysis(
    [str(Path(SPECPATH) / f"{entry}.py") for entry in (WINDOW, CONSOLE)],
    datas=[
        *collect_data_files("djs"),         # schema.sql and the UI files
        *collect_data_files("playwright"),  # its driver (node + JS): browser capture and login need it
        *copy_metadata("mcp"),              # mcp reads its own version at import
        *copy_metadata("dear-job-seeker"),  # the `status` tool reports this version
    ],
    hiddenimports=collect_submodules("djs"),  # platforms are imported by name
)
pyz = PYZ(analysis.pure)


def scripts(entry):
    """The runtime hooks plus this program's entry script, without the other program's."""
    other = CONSOLE if entry == WINDOW else WINDOW
    return [s for s in analysis.scripts if s[0] != other]


window = EXE(pyz, scripts(WINDOW), OPTIONS, exclude_binaries=True, name=NAME, console=False, upx=False)
console = EXE(pyz, scripts(CONSOLE), OPTIONS, exclude_binaries=True, name="djs", console=True, upx=False)
folder = COLLECT(window, console, analysis.binaries, analysis.datas, name=FOLDER, upx=False)
BUNDLE(
    folder,
    name=f"{NAME}.app",
    bundle_identifier=BUNDLE_ID,
    version=VERSION,
    # BUNDLE takes the alphabetically first program (`djs`) as the app's main one and, as that one is a console
    # program, makes the app background-only: say explicitly that double-clicking opens the window
    info_plist={"CFBundleExecutable": NAME, "LSBackgroundOnly": False, "NSHighResolutionCapable": True},
)
