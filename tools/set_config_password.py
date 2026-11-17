"""Set the independent model-settings password locally; never accept it in command-line arguments."""
import argparse
from getpass import getpass
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, help='External Web data directory')
    args = parser.parse_args()
    if args.data_dir:
        os.environ['KANDU_DATA_DIR'] = str(args.data_dir.resolve())
    from kandu import core_config as config, core_parsers as store
    from werkzeug.security import generate_password_hash
    root = config.app_dir()
    password = getpass('New model-settings password: ')
    repeat = getpass('Confirm model-settings password: ')
    if password != repeat:
        parser.error('Passwords do not match; no changes made')
    if not 8 <= len(password) <= 200:
        parser.error('Use 8 to 200 characters; no changes made')
    store._atomic_json_write(root / 'config-auth.json', dict(passwordHash=generate_password_hash(password)))
    print('Model-settings password hash saved outside source. Previous unlocks are invalidated.')


if __name__ == '__main__':
    main()
