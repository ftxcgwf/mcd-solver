# -*- coding: utf-8 -*-
"""
口感先验：把「老饕的嘴」变成可计算的向量。

这是本项目区别于纯数据工具的核心。麦当劳 MCP 能给你热量和钠，
但它永远不会告诉你——

    一份餐里同时有「酥脆 + 多汁 + 冰爽」三种口感时，
    满足感远高于三样都是软塌塌的东西。

这种判断来自经验，不来自数据库。本模块把它工程化成标签向量，
让求解器可以像人一样权衡「这顿吃得爽不爽」。

标注策略：
  1. 规则匹配（关键词 -> 口感标签），覆盖全量餐品，可复现
  2. data/taste_override.json 手工精修，规则误判时可覆盖
"""

import json
import os

# 口感维度。注意：crispy/juicy/cold 这类是「体验」，不是成分
TEXTURES = [
    "crispy",   # 酥脆：薯条、鸡块、麦辣鸡翅
    "juicy",    # 多汁：板烧鸡腿堡、安格斯牛肉饼
    "tender",   # 嫩滑：猪柳、鸡腿排
    "cold",     # 冰爽：可乐、圆筒、雪冰
    "hot",      # 温热：现炸、热咖啡、玉米杯
    "creamy",   # 奶香：芝士、圆筒、拿铁、奶昔
    "chewy",    # 嚼劲：面包、卷饼
    "sweet",    # 甜：派、麦旋风、菠萝派
    "salty",    # 咸香：薯条、鸡块
    "fresh",    # 清爽：沙拉、玉米、生菜
]

TEXTURE_ZH = {
    "crispy": "酥脆", "juicy": "多汁", "tender": "嫩滑", "cold": "冰爽",
    "hot": "温热", "creamy": "奶香", "chewy": "嚼劲", "sweet": "甜",
    "salty": "咸香", "fresh": "清爽",
}

# 关键词 -> 口感标签。同时匹配品名与官网描述，描述往往更准
RULES = {
    "crispy": ["薯条", "薯格", "鸡块", "鸡翅", "麦辣", "脆", "酥", "鸡排", "薯片", "洋葱圈", "派"],
    "juicy": ["鸡腿堡", "板烧", "牛肉", "安格斯", "猪柳", "多汁", "肉饼", "鸡腿"],
    "tender": ["鸡腿", "板烧", "猪柳", "嫩", "鸡蛋", "蛋"],
    "cold": ["可乐", "雪碧", "冰", "圆筒", "麦旋风", "冰淇淋", "雪冰", "冷饮", "冰茶", "气泡", "柠檬", "橙汁", "牛奶"],
    "hot": ["咖啡", "热", "玉米", "汤", "拿铁", "美式", "红茶", "豆浆"],
    "creamy": ["奶", "芝士", "起司", "圆筒", "麦旋风", "冰淇淋", "拿铁", "奶昔", "巧克力", "卡布"],
    "chewy": ["汉堡", "堡", "卷", "饼", "面包"],
    "sweet": ["派", "圆筒", "麦旋风", "冰淇淋", "甜", "巧克力", "菠萝", "香芋", "奶昔", "蛋糕", "枫糖", "松饼"],
    "salty": ["薯条", "鸡块", "咸", "盐", "辣"],
    "fresh": ["莎拉", "沙拉", "玉米", "生菜", "番茄", "蔬菜", "苹果"],
}

# 餐品分类 -> 用餐角色。一顿饭的结构感由此而来
ROLE_BY_CATEGORY = {
    "burger": "main",
    "breakfast": "main",
    "happy_meals": "main",
    "mccafe": "drink",
    "beverage": "drink",
    "snacks": "side",
    "dessert": "dessert",
}

ROLE_ZH = {"main": "主食", "side": "小食", "drink": "饮料", "dessert": "甜品"}

# 各角色的基础满足分（10 分制）。主食是一顿饭的锚点，甜品是点缀
ROLE_BASE = {"main": 6.5, "side": 5.0, "drink": 3.2, "dessert": 4.8}

# 口感标签对满足感的加成。酥脆和多汁是最强的两个愉悦来源
TEXTURE_BONUS = {
    "crispy": 0.9, "juicy": 0.8, "tender": 0.4, "cold": 0.3,
    "hot": 0.2, "creamy": 0.5, "chewy": 0.1, "sweet": 0.4,
    "salty": 0.3, "fresh": 0.2,
}


def load_overrides(path="data/taste_override.json"):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def infer_textures(name, desc="", overrides=None):
    """
    推断口感标签集合。手工覆盖优先。

    只用品名匹配，不用官网描述。原因：描述是营销文案，动辄出现
    「新鲜生菜」「香甜」「冰爽」等词，会让麦满分同时被打上七个标签、
    让热奶铁既是「温热」又是「冰爽」。宁可标签少而准，也不要多而乱。
    确有需要时用 data/taste_override.json 手工补。
    """
    if overrides and name in overrides:
        return set(t for t in overrides[name] if t in TEXTURES)

    tags = set()
    for tag, words in RULES.items():
        for w in words:
            if w in name:
                tags.add(tag)
                break
    if not tags and "堡" in name:
        tags.add("chewy")

    # 去歧义：「冰美式」同时命中「冰」和「美式」，显然是冷的；
    # 「热奶铁」同时命中「热」和「奶」，显然是热的。以显式温度词为准。
    if "冰" in name:
        tags.discard("hot")
        tags.add("cold")
    elif "热" in name:
        tags.discard("cold")
        tags.add("hot")
    elif "cold" in tags and "hot" in tags:
        # 没有显式温度词却冷热皆中（如「阳光柠檬红茶」），
        # 按麦当劳饮品默认冰饮处理
        tags.discard("hot")
    return tags


def base_score(role, textures):
    """单品固有满足度（10 分制）。"""
    s = ROLE_BASE.get(role, 5.0)
    for t in textures:
        s += TEXTURE_BONUS.get(t, 0.0)
    return round(min(s, 10.0), 2)


def is_cold(textures):
    return "cold" in textures


def is_hot(textures):
    return "hot" in textures
