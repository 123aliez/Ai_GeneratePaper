"""程序生成物的落点。

项目模式下统一写在 `_work/index/`,作者的 inbox 保持只读;未指定项目(单元测试)时
退回结果目录下,免得测试往仓库里写文件。
"""
from pathlib import Path


def res_catalog_path(data_root) -> Path:
    import config
    return Path(config.RES_IDS_PATH) if config.HAS_PROJECT else Path(data_root) / "res-ids.md"


def data_index_path(data_root) -> Path:
    import config
    return Path(config.DATA_INDEX_PATH) if config.HAS_PROJECT else Path(data_root) / "data-index.md"


def settings_path(data_root) -> Path:
    import config
    return Path(data_root) / config.SETTINGS_FILENAME
