from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry


class Config(AttrDocModel):
    model_updater_template_file: str = "template.html"
    """模型更新器使用的HTML模板文件名，支持HTML格式。"""


ConfigRegistry.register_config_types(Config)
