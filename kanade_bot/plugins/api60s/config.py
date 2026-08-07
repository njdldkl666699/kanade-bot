from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry


class Config(AttrDocModel):
    api60s_base_url: str = "https://60s.viki.moe"
    """60s API Base URL"""


ConfigRegistry.register_config_types(Config)
