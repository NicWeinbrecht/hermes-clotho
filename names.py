"""Every user-visible name in one place, so a rename is this file, plugin.yaml and a grep."""

PLUGIN_NAME = "clotho"                   # manifest name, catalog key, plugin-data folder, temp-file/tag prefix
PRODUCT = "Clotho"                       # product name in text
CLI_NAME = "clotho"                      # `hermes clotho ...`
SLASH_NAME = "clotho"                    # `/clotho on|off|report`
RESULT_TAG = "[clotho]"                  # prefix of every simulated tool result
LEDGER_SCHEMA = "clotho.ledger.v1"
META_SCHEMA = "clotho.meta.v1"
RECEIPT_SCHEMA = "clotho.receipt.v1"
ENV_PREFIX = "CLOTHO_"                   # environment variables read by the plugin
SANDBOX_ENV = "CLOTHO"                   # set to 1 inside sandboxed commands
SANDBOX_PROFILE = "clotho.sb"            # macOS sandbox profile next to this file
REPO_URL = "https://github.com/NicWeinbrecht/hermes-clotho"
RID_PREFIX = "r-"                        # rehearsal ids look like r-20261110-061500-ab12
FORK_PREFIX = "f-"                       # fork ids look like f-20261110-101500-ab12
FORKS_DIR = "forks"                      # fork records, under the plugin data folder
VERSION = "0.1.0"

PITCH = "Spin every future. Keep one."


def env(name: str) -> str:
    return ENV_PREFIX + name
