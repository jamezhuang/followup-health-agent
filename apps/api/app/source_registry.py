"""官网来源名单。添加机构前，请核对其真实官网域名。"""

OFFICIAL_SITES = {
    "zjyy.com.cn": "南方医科大学珠江医院",
    "sshospital.cn": "佛山市三水区人民医院",
    "pumch.cn": "北京协和医院",
    "pkufh.com": "北京大学第一医院",
    "zy91.com": "浙江大学医学院附属第一医院",
    "wchscu.cn": "四川大学华西医院",
    "fahsysu.org.cn": "中山大学附属第一医院",
    "qiluhospital.com": "山东大学齐鲁医院",
    "dyyy.xjtu.edu.cn": "西安交通大学第一附属医院",
    "nhc.gov.cn": "国家卫生健康委员会",
    "chinacdc.cn": "中国疾病预防控制中心",
}

# Verified against https://www.sshospital.cn/ on 2026-10-19. Exact names only:
# the Guangzhou parent hospital and the Sanshui institution have different hosts.
OFFICIAL_ALIASES = {
    "南方医科大学珠江医院三水医院": "佛山市三水区人民医院",
    "珠江医院三水医院": "佛山市三水区人民医院",
    "三水区人民医院": "佛山市三水区人民医院",
}


def official_sites_for_names(names):
    canonical = {OFFICIAL_ALIASES.get(name, name) for name in names}
    return {host: name for host, name in OFFICIAL_SITES.items() if name in canonical}


def normalize_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host
