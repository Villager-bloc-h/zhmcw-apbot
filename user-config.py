"""Pywikibot user configuration for zh.minecraft.wiki (Minecraft Wiki).

zh.minecraft.wiki is not a bundled pywikibot family, so AutoFamily builds the
site from the API endpoint below.

The bot account name can be overridden with the MCW_BOT_USER environment
variable (useful for forks).  The password itself is NOT here: it lives in
password.py (git-ignored), see password.py.example.
"""

import os

mylang = 'minecraft'
family = 'minecraft'

# api.php sits at the wiki root; AutoFamily derives scriptpath/protocol.
family_files['minecraft'] = 'https://zh.minecraft.wiki/api.php'

# Bot account *without* the @suffix; the suffix is part of password.py.
usernames['minecraft']['minecraft'] = os.environ.get(
    'MCW_BOT_USER', 'Villager-bloc-h')

# Credentials file, one tuple per line.
password_file = 'password.py'

# Be polite.
put_throttle = 5
cosmetic_changes = False
