"""Install a per-user menu shortcut without hardcoded user names."""
import os
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
def escape(value):
    return str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')

content = '[Desktop Entry]\nType=Application\nName=Контур\nComment=Визуализация целей\nExec=/bin/sh "{}"\nIcon=applications-science\nTerminal=false\nCategories=Science;\n'.format(escape(root / 'launch-astra.sh'))
menu = Path.home() / '.local/share/applications'
menu.mkdir(parents=True, exist_ok=True)
path = menu / 'kontur.desktop'
path.write_text(content, encoding='utf-8')
path.chmod(0o755)
try:
    desktop = subprocess.check_output(['xdg-user-dir', 'DESKTOP'], universal_newlines=True).strip()
    if desktop and Path(desktop).is_dir() and Path(desktop) != Path.home():
        shortcut = Path(desktop) / 'Контур.desktop'
        shortcut.write_text(content, encoding='utf-8')
        shortcut.chmod(0o755)
except (OSError, subprocess.CalledProcessError):
    pass
print('Ярлык установлен:', path)
