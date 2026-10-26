"""行情消息板块概念知识库（行情消息 P0-3）。

背景：概念热点汇总原先是纯前端实现（frontend/src/components/MarketNews.tsx
内的 SECTOR_CONCEPTS + mapToSectors），统计口径只覆盖"当次拉取的 ≤200 条
消息"，且列表筛选状态会污染热点口径。P0-3 将知识库下沉到后端：

1. 采集入库时把匹配到的板块 id 写入 market_events.affected_sectors
   （逗号分隔，激活原先一直闲置的字段），让板块标签成为可查询的落库数据；
2. GET /market-events/hotspots 用同一套知识库在后端做窗口聚合，
   统计口径与列表筛选彻底解耦。

关键词清单与前端 MarketNews.tsx 的 SECTOR_CONCEPTS 保持一致（逐字平移），
避免两端口径漂移；前端仍保留一份知识库用于展示元数据（图标/颜色/名称）
和后端接口异常时的本地降级统计。

匹配规则与前端 mapToSectors 相同：标题+摘要做 contains 匹配，
每条消息最多归属 3 个板块。
"""
from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import select


class SectorConcept(NamedTuple):
    id: str
    keywords: tuple[str, ...]


# 与 frontend/src/components/MarketNews.tsx SECTOR_CONCEPTS 逐字对齐（仅关键词，展示元数据留在前端）
SECTOR_CONCEPTS: list[SectorConcept] = [
    # ===== 科技/AI =====
    SectorConcept("ai_compute", ("人工智能", "AI", "大模型", "GPT", "ChatGPT", "GPU", "算力", "数据中心", "云计算", "英伟达", "NVIDIA", "芯片", "半导体", "台积电", "英特尔", "AMD", "微软", "OpenAI", "深度学习", "机器学习", "推理", "训练", "H100", "A100", "服务器", "超算")),
    SectorConcept("semiconductor", ("半导体", "芯片", "晶圆", "光刻", "存储器", "DRAM", "NAND", "闪存", "集成电路", "IC设计", "封测", "中芯", "华虹", "三星电子", "SK海力士", "美光", "高通", "博通", "联发科")),
    SectorConcept("software_saas", ("软件", "SaaS", "云服务", "办公软件", "操作系统", "数据库", "ERP", "CRM", "Oracle", "Salesforce", "Adobe", "用友", "金山", "金蝶", "微软", "谷歌")),
    SectorConcept("consumer_electronics", ("消费电子", "手机", "iPhone", "华为", "小米", "苹果", "平板", "可穿戴", "AR", "VR", "元宇宙", "Meta", "耳机", "智能家居", "IoT")),
    SectorConcept("space_satellite", ("航天", "卫星", "SpaceX", "火箭", "太空", "NASA", "星链", "北斗", "GPS", "马斯克", "贝索斯", "商业航天", "低轨卫星", "发射", "轨道")),
    # ===== 新能源 =====
    SectorConcept("new_energy_vehicle", ("新能源车", "电动汽车", "EV", "特斯拉", "Tesla", "比亚迪", "蔚来", "理想", "小鹏", "问界", "宁德时代", "CATL", "动力电池", "锂电", "充电桩", "自动驾驶", "智驾", "固态电池", "4680", "刀片电池")),
    SectorConcept("solar_pv", ("光伏", "太阳能", "硅料", "硅片", "组件", "逆变器", "隆基绿能", "通威股份", "阳光电源", "天合光能", "晶科能源", "HJT", "TOPCon", "钙钛矿")),
    SectorConcept("wind_power", ("风电", "风力发电", "风机", "叶片", "海上风电", "陆上风电", "金风科技", "明阳智能", "运达股份", "三峡能源")),
    SectorConcept("energy_storage", ("储能", "钠离子", "液流电池", "压缩空气", "抽水蓄能", "电网侧储能", "户用储能", "派能科技", "鹏辉能源")),
    # ===== 贵金属/大宗商品 =====
    SectorConcept("precious_metals", ("黄金", "白银", "金价", "银价", "避险", "COMEX", "伦敦金", "现货黄金", "ETF黄金", "央行购金", "珠宝首饰", "铂金", "钯金", "紫金矿业", "山东黄金")),
    SectorConcept("crude_oil_energy", ("原油", "石油", "OPEC", "EIA", "油价", "WTI", "布伦特", "天然气", "页岩油", "沙特", "俄罗斯石油", "炼油", "成品油", "汽油", "柴油", "中石油", "中石化", "中海油", "埃克森美孚", "雪佛龙")),
    SectorConcept("steel_metals", ("钢铁", "铁矿石", "螺纹钢", "热卷板", "铜", "铝", "锌", "镍", "锡", "铅", "LME", "SHMET", "上海金属", "宝钢", "河钢", "五矿", "江西铜业", "紫金矿业", "洛阳钼业")),
    SectorConcept("agriculture", ("农产品", "大豆", "玉米", "小麦", "棉花", "糖", "橡胶", "棕榈油", "豆粕", "生猪", "猪肉", "粮食", "化肥", "农药", "种业", "北大荒", "中粮")),
    # ===== 金融地产 =====
    SectorConcept("real_estate", ("房地产", "楼市", "房价", "恒大", "碧桂园", "万科", "保利", "龙湖", "融创", "房贷", "LPR", "土地出让", "二手房", "新房销售", "物业", "保障房", "城中村")),
    SectorConcept("banking_finance", ("银行", "利率", "降息", "加息", "存款", "贷款", "信贷", "M2", "MLF", "SLF", "逆回购", "LPR", "央行", "美联储", "欧央行", "日央行", "四大行", "招行", "平安银行", "券商", "保险")),
    SectorConcept("insurance", ("保险", "寿险", "财险", "中国人寿", "中国平安", "太保", "新华保险", "保费", "赔付率", "偿付能力", "养老险", "健康险")),
    # ===== 医药生物 =====
    SectorConcept("pharma_biotech", ("医药", "制药", "创新药", "仿制药", "CRO", "CDMO", "疫苗", "中药", "恒瑞医药", "药明康德", "迈瑞医疗", "爱尔眼科", "通策医疗", "CXO", "ADC", "GLP", "减肥药")),
    SectorConcept("medical_devices", ("医疗器械", "影像设备", "高值耗材", "IVD", "体外诊断", "联影医疗", "迈瑞", "乐普医疗", "微创医疗", "骨科植入", "心血管支架")),
    # ===== 消费 =====
    SectorConcept("food_beverage", ("白酒", "啤酒", "乳制品", "调味品", "预制菜", "零食", "茅台", "五粮液", "伊利", "海天味业", "涪陵榨菜", "安井食品", "餐饮", "海底捞", "星巴克", "可口可乐", "百事")),
    SectorConcept("retail_ecommerce", ("电商", "零售", "直播带货", "拼多多", "京东", "阿里巴巴", "淘宝", "天猫", "亚马逊", "抖音电商", "美团", "免税", "中国中免", "永辉超市", "苏宁")),
    SectorConcept("tourism_hotel", ("旅游", "酒店", "航空", "机场", "景区", "免税店", "携程", "同程", "中国中免", "锦江酒店", "首旅酒店", "迪士尼", "主题公园", "邮轮", "出境游")),
    SectorConcept("auto_chain", ("汽车", "整车", "零部件", "特斯拉供应链", "比亚迪产业链", "造车新势力", "自动驾驶", "激光雷达", "毫米波雷达", "线控底盘", "一体化压铸", "拓普集团", "德赛西威", "华域汽车")),
    # ===== 军工/高端制造 =====
    SectorConcept("defense_military", ("军工", "国防", "导弹", "战斗机", "航母", "无人机", "军机", "舰船", "潜艇", "中航沈飞", "航发动力", "中国船舶", "洪都航空", "北方导航", "信息化装备", "C919", "大飞机")),
    SectorConcept("robotics", ("机器人", "人形机器人", "工业机器人", "协作机器人", "伺服电机", "减速器", "传感器", "特斯拉Optimus", "优必选", "埃斯顿", "汇川技术", "具身智能")),
    SectorConcept("advanced_mfg", ("数控机床", "工业母机", "激光设备", "3D打印", "增材制造", "工程机械", "三一重工", "中联重科", "徐工机械", "格力电器", "美的集团", "海尔智家")),
    # ===== 宏观/政策 =====
    SectorConcept("macro_policy_cn", ("国务院", "发改委", "财政部", "央行", "证监会", "政治局", "两会", "GDP目标", "财政政策", "货币政策", "减税降费", "专项债", "赤字率", "社融", "M2", "PMI", "CPI", "PPI", "稳增长", "扩内需", "促消费")),
    SectorConcept("macro_intl_geo", ("地缘政治", "贸易战", "关税", "制裁", "美联储", "欧洲央行", "日本央行", "G7", "G20", "IMF", "世界银行", "WTO", "RCEP", "一带一路", "中美关系", "中俄关系", "中欧关系", "俄乌冲突", "巴以冲突", "红海危机")),
    SectorConcept("crypto_web3", ("比特币", "以太坊", "BTC", "ETH", "区块链", "Web3", "DeFi", "NFT", "稳定币", "SEC", "币安", "Coinbase", "挖矿", "矿机", "ETF", "现货比特币", "监管")),
    # ===== 其他 =====
    SectorConcept("telecom_5g", ("5G", "6G", "通信", "运营商", "中国移动", "中国联通", "中国电信", "光纤", "基站", "光模块", "中兴通讯", "烽火通信", "华为", "网络建设", "算力网络")),
    SectorConcept("media_entertainment", ("影视", "游戏", "短视频", "抖音", "快手", "腾讯游戏", "网易游戏", "电影票房", "院线", "芒果TV", "爱奇艺", "腾讯视频", "字节跳动", "广告营销")),
    SectorConcept("environment_carbon", ("碳中和", "碳交易", "环保", "ESG", "绿色金融", "碳达峰", "新能源配储", "CCUS", "碳排放权", "清洁能源转型", "光伏治沙", "林业碳汇")),
]

SECTOR_IDS: frozenset[str] = frozenset(concept.id for concept in SECTOR_CONCEPTS)

# 关键词→板块倒排索引（构建一次，进程内复用）。同一关键词只归属首个板块，与前端行为一致。
KEYWORD_TO_SECTOR: dict[str, str] = {}
for _concept in SECTOR_CONCEPTS:
    for _kw in _concept.keywords:
        KEYWORD_TO_SECTOR.setdefault(_kw, _concept.id)

MAX_SECTORS_PER_EVENT = 3


def map_to_sectors(title: str, summary: str | None) -> list[str]:
    """将消息标题+摘要映射到相关板块 id（最多 MAX_SECTORS_PER_EVENT 个）。

    与前端 mapToSectors 行为一致：contains 匹配、倒排索引顺序、上限 3。
    """
    text = f"{title} {summary or ''}"
    matched: list[str] = []
    seen: set[str] = set()
    for keyword, sector_id in KEYWORD_TO_SECTOR.items():
        if keyword in text and sector_id not in seen:
            seen.add(sector_id)
            matched.append(sector_id)
            if len(matched) >= MAX_SECTORS_PER_EVENT:
                break
    return matched


def parse_affected_sectors(field: str | None) -> list[str]:
    """解析落库的 affected_sectors 字段（逗号分隔板块 id），未知 id 丢弃。"""
    if not field:
        return []
    return [item.strip() for item in field.split(",") if item.strip() in SECTOR_IDS]


__all__ = [
    "SECTOR_CONCEPTS",
    "SECTOR_IDS",
    "MAX_SECTORS_PER_EVENT",
    "map_to_sectors",
    "parse_affected_sectors",
]


# ── P2-12：DB 覆盖合并与 CRUD ───────────────────────────────


def resolve_concepts(db) -> list[SectorConcept]:
    """内置默认 + DB 覆盖（sector_id 整行替换关键词）。

    DB 读取失败/表缺失时回退内置——调词是运营增强，不阻断采集与热点。
    """
    defaults = {concept.id: concept for concept in SECTOR_CONCEPTS}
    try:
        from app.models.market_event import SectorConceptConfig

        for row in db.execute(select(SectorConceptConfig)).scalars().all():
            words = [w.strip() for w in (row.keywords or "").split(",") if w.strip()]
            if words:
                defaults[row.sector_id] = SectorConcept(row.sector_id, tuple(words))
    except Exception:
        pass
    return list(defaults.values())


def build_index(concepts: list[SectorConcept]) -> dict[str, str]:
    """倒排索引：同一关键词只归属首个板块（与内置构建规则一致）。"""
    index: dict[str, str] = {}
    for concept in concepts:
        for kw in concept.keywords:
            index.setdefault(kw, concept.id)
    return index


def map_sectors_on(index: dict[str, str], title: str, summary: str | None) -> list[str]:
    """在给定索引上做板块匹配（与 map_to_sectors 同规则：contains、上限 3）。"""
    text = f"{title} {summary or ''}"
    matched: list[str] = []
    seen: set[str] = set()
    for keyword, sector_id in index.items():
        if keyword in text and sector_id not in seen:
            seen.add(sector_id)
            matched.append(sector_id)
            if len(matched) >= MAX_SECTORS_PER_EVENT:
                break
    return matched


def map_to_sectors_db(db, title: str, summary: str | None) -> list[str]:
    """带 DB 覆盖的匹配入口（采集打标 / 热点聚合使用）。"""
    return map_sectors_on(build_index(resolve_concepts(db)), title, summary)


def list_sector_concept_configs(db) -> list[dict]:
    """合并视图：全部内置板块 + DB 是否覆盖标记（后台管理页签数据源）。"""
    from app.models.market_event import SectorConceptConfig

    overrides = {
        row.sector_id: row for row in db.execute(select(SectorConceptConfig)).scalars().all()
    }
    result: list[dict] = []
    seen: set[str] = set()
    for concept in SECTOR_CONCEPTS:
        seen.add(concept.id)
        row = overrides.get(concept.id)
        if row is None:
            result.append({"sector_id": concept.id, "keywords": list(concept.keywords), "source": "builtin"})
        else:
            words = [w.strip() for w in (row.keywords or "").split(",") if w.strip()]
            result.append({"sector_id": concept.id, "keywords": words, "source": "db"})
    # DB 中新增的自定义板块（不在内置清单里）
    for sector_id, row in overrides.items():
        if sector_id not in seen:
            result.append({
                "sector_id": sector_id,
                "keywords": [w.strip() for w in (row.keywords or "").split(",") if w.strip()],
                "source": "db",
            })
    return result


def update_sector_concept_config(db, sector_id: str, keywords: list[str], *, updated_by: str | None = None) -> dict:
    """P2-12：覆盖某板块关键词（整行替换）；空列表=删除覆盖恢复内置。

    未知 sector_id 允许创建（自定义板块）；但热点面板展示依赖前端内置
    元数据，自定义板块仅参与匹配聚合。
    """
    from app.models.market_event import SectorConceptConfig

    sector_id = (sector_id or "").strip()
    if not sector_id or len(sector_id) > 64:
        raise ValueError("sector_id 必须为 1-64 字符")
    words: list[str] = []
    for word in keywords:
        text = str(word).strip()
        if text:
            words.append(text)
        if len(words) >= 200:
            break
    row = db.execute(
        select(SectorConceptConfig).where(SectorConceptConfig.sector_id == sector_id)
    ).scalars().first()
    if not words:
        if row is not None:
            db.delete(row)
            db.commit()
        return {"sector_id": sector_id, "keywords": [], "source": "builtin"}
    if row is None:
        row = SectorConceptConfig(sector_id=sector_id, keywords=",".join(words), updated_by=updated_by)
        db.add(row)
    else:
        row.keywords = ",".join(words)
        row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    return {"sector_id": sector_id, "keywords": words, "source": "db"}
