# The GitHub repository whose releases and installer LoopX installs from.
LOOPX_SOURCE_REPOSITORY = "foolzzz/loopx"
LOOPX_SOURCE_CLONE_URL = f"https://github.com/{LOOPX_SOURCE_REPOSITORY}.git"

NO_CLONE_INSTALL_URL = (
    f"https://raw.githubusercontent.com/{LOOPX_SOURCE_REPOSITORY}/main/install.sh"
)

DEFAULT_INSTALL_COMMAND = f"curl -fsSL {NO_CLONE_INSTALL_URL} | bash"
DEFAULT_WORKFLOW_SKILL_INSTALL_COMMAND = "loopx workflow-skills --install"
DEFAULT_INSTALL_REPAIR_COMMAND = (
    f"{DEFAULT_INSTALL_COMMAND}\n"
    f"{DEFAULT_WORKFLOW_SKILL_INSTALL_COMMAND}\n"
    "loopx doctor"
)
ARCHIVE_FALLBACK_INSTALL_COMMAND = (
    f"curl -fsSL {NO_CLONE_INSTALL_URL} | bash\n"
    'export PATH="$HOME/.local/bin:$PATH"\n'
    "loopx doctor"
)
