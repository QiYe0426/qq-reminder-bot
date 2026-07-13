import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from plugins.logging_privacy import configure_log_privacy


nonebot.init()
configure_log_privacy()

driver = nonebot.get_driver()
driver.register_adapter(OneBotV11Adapter)

nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
