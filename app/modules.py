"""App 里可以按模块开关同步的模块。iOS 新增模块时在这里加一个名字即可。"""

MODULES: tuple[str, ...] = (
    "fitness",  # 健身
    "focus",  # 专注
    "sleep",  # 睡眠
    "diary",  # 日记
    "screen",  # 屏幕时间
    "word",  # 单词
    "speak",  # 口才
    "love",  # 恋爱（含照片、语音）
    "growth",  # 团子成长
)
