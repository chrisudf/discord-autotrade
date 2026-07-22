"""
App 层配置(新模块,契约新增)。

frozen dataclass Settings + load_settings(env_path) -> Settings。

校验策略:一次性收集**所有**错误再退出(而不是碰到第一个就 exit),
避免"改一个 env 重启一次才发现下一个错"的循环。

注意(本期范围):broker.common 的模块级常量、watcher / risk 的 per-call
os.getenv 语义**本期保留**,Settings 只服务 app 层(组合根)。两边读的是
同一份 os.environ,load_dotenv 之后天然一致。
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from autotrade.utils.logger import logger

# 仓库根(autotrade/settings.py → parents[1])
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    discord_token: str
    dry_run: bool
    trd_env: str          # "SIMULATE" / "REAL"(已 strip().upper())
    acc_id: int
    moomoo_host: str
    moomoo_port: int
    trade_pwd: str
    tg_token: str
    tg_chat_id: str
    log_dir: Path
    data_dir: Path


def load_settings(env_path: Optional[Path] = None) -> Settings:
    """读 config/.env(可选)+ os.environ → Settings。

    env_path 非 None 时在此处 load_dotenv(override=True)——这是全应用唯一
    的 .env 加载点,由 app.main(组合根)在 main() 里调用;库代码一律禁止
    import-time load_dotenv。

    校验失败:log 出**全部**错误后 raise SystemExit(1)。
    """
    if env_path is not None:
        load_dotenv(env_path, override=True)

    errors: list[str] = []

    discord_token = os.getenv("DISCORD_USER_TOKEN", "").strip()
    if not discord_token:
        errors.append("DISCORD_USER_TOKEN 未配置(config/.env)")

    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"

    # .strip().upper() 防止 .env 写成 "simulate" / " REAL " 之类导致
    # 下游所有 == 判断失效(与 broker.common 口径一致)
    trd_env = os.getenv("MOOMOO_TRD_ENV", "SIMULATE").strip().upper()
    if trd_env not in ("SIMULATE", "REAL"):
        errors.append(f"MOOMOO_TRD_ENV={trd_env!r} 无效,只接受 SIMULATE / REAL")

    moomoo_host = os.getenv("MOOMOO_HOST", "127.0.0.1")

    moomoo_port = 11111
    _port_raw = os.getenv("MOOMOO_PORT", "11111")
    try:
        moomoo_port = int(_port_raw)
    except ValueError:
        errors.append(f"MOOMOO_PORT={_port_raw!r} 不是整数")

    acc_id = 0
    _acc_raw = os.getenv("MOOMOO_ACC_ID", "0")
    try:
        acc_id = int(_acc_raw)
    except ValueError:
        errors.append(f"MOOMOO_ACC_ID={_acc_raw!r} 不是整数")
    # 实盘/模拟盘真单模式都需要 ACC_ID。空着启动 → 接到信号才报错的悲剧
    # (6/18 IWM 卖单失败就是这个原因)。preflight 里保留同源检查作为第二道防线,
    # 这里提前到配置层,和其它错误一起报。
    if not dry_run and acc_id == 0:
        errors.append(
            "DRY_RUN=false 但 MOOMOO_ACC_ID 未配置或为 0。"
            "检查 config/.env 里 MOOMOO_ACC_ID=<数字> 是否存在且非零。"
        )

    # .env 里统一 MOOMOO_TRD_* 前缀(TRD_ENV / TRD_PWD)。
    # 兼容老 .env 里的 MOOMOO_TRADE_PWD:旧名字命中时打个 warn 提示迁移
    # (与 broker.common 的兼容逻辑同口径)
    _legacy_pwd = os.getenv("MOOMOO_TRADE_PWD")
    trade_pwd = os.getenv("MOOMOO_TRD_PWD") or _legacy_pwd or ""
    if _legacy_pwd and not os.getenv("MOOMOO_TRD_PWD"):
        logger.warning(
            "[settings] 检测到旧 env 名 MOOMOO_TRADE_PWD，建议改为 MOOMOO_TRD_PWD（见 config/.env.example）"
        )

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # 路径:缺省 = 仓库根下 logs/ 与 data/。
    # 注意:本期 storage 的 DB_PATH 仍是各模块内常量,不消费 DATA_DIR;
    # data_dir 仅供 app 层使用/展示。
    log_dir = Path(os.getenv("LOG_DIR", str(_REPO_ROOT / "logs")))
    data_dir = Path(os.getenv("DATA_DIR", str(_REPO_ROOT / "data")))

    if errors:
        for e in errors:
            logger.error(f"[settings] ❌ {e}")
        logger.error(
            f"[settings] 共 {len(errors)} 个配置错误，修复 config/.env 后重启"
        )
        raise SystemExit(1)

    return Settings(
        discord_token=discord_token,
        dry_run=dry_run,
        trd_env=trd_env,
        acc_id=acc_id,
        moomoo_host=moomoo_host,
        moomoo_port=moomoo_port,
        trade_pwd=trade_pwd,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        log_dir=log_dir,
        data_dir=data_dir,
    )
