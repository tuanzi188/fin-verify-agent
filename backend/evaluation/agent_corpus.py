"""能力评测语料 = 现有 33 chunk（`sample_data.CORPUS`）+ 9 份注入文档 27 chunk。

**为什么不直接改 `sample_data.py`**：那 33 chunk 是检索回归夹具（60 条 query）的语料，
`baseline_retrieval.json` 的指标是对着它算的。动它等于让 CI 基线失效。
所以这里做**增量叠加**：回归用 `CORPUS`，能力评测用 `AGENT_CORPUS`。

注入清单（文档 §9.2，2026-10-27 定）：

| 类型 | 文档 | 冲突/缺口点 |
|---|---|---|
| 冲突 | d12 差旅住宿标准补充通知 | 住宿上限 600 vs d2 的 500 |
| 冲突 | d13 报销审批权限调整通知 | 审批门槛 3000 vs d2 的 1000 |
| 冲突 | d14 年终奖系数调整通知 | A/B 档系数 2.5/1.5 vs d9 的 2.0/1.2 |
| 缺口 | d15 加班与调休规定 | 只写「按国家规定」与计算基数，不给倍数 |
| 缺口 | d18 考勤管理细则 | 全勤奖金额、迟到扣款金额都写「由部门确定」 |
| 近义 | d17 培训与晋升补充 | 内部转岗 / 跨部门借调 / 岗位晋升 三者条件不同 |
| 近义 | d19 居家办公细则 | 居家办公 vs 例行远程办公，报备方式不同 |
| 易混数字 | d16 员工福利概览（2025） | 体检 800 / 礼金 1000 |
| 易混数字 | d20 员工福利调整通知（2026） | 体检 1200 / 礼金 1500 |

冲突**不设权威**（2026-10-27 定）：d12/d13/d14 只声明自己的适用范围，不写「旧文件废止」，
因此"以哪份为准"在语料内不可判定；对应任务只判"是否指出两份文件不一致"。
"""

import hashlib

from evaluation.sample_data import CORPUS

AGENT_CORPUS_VERSION = "v1"

INJECTED_CHUNKS = [
    # d12 差旅住宿标准补充通知（与 d2_ch1 冲突）
    {"chunk_id": "d12_ch1", "doc_id": "d12", "title": "差旅住宿标准补充通知",
     "content": "本通知自 2026 年 1 月 1 日起执行，适用于该日期之后出发的出差。员工出差住宿标准为每晚不超过六百元。"},
    {"chunk_id": "d12_ch2", "doc_id": "d12", "title": "差旅住宿标准补充通知",
     "content": "一线城市在本通知标准基础上可上浮百分之二十，即每晚不超过七百二十元；其他城市不设上浮。"},
    {"chunk_id": "d12_ch3", "doc_id": "d12", "title": "差旅住宿标准补充通知",
     "content": "住宿费需凭酒店开具的发票报销，发票抬头须为公司全称；超出标准的部分由个人承担。"},

    # d13 报销审批权限调整通知（与 d2_ch2 冲突）
    {"chunk_id": "d13_ch1", "doc_id": "d13", "title": "报销审批权限调整通知",
     "content": "自 2026 年 4 月 1 日起，单次报销金额低于三千元由部门负责人审批。"},
    {"chunk_id": "d13_ch2", "doc_id": "d13", "title": "报销审批权限调整通知",
     "content": "单次报销金额达到三千元及以上需财务总监审批；达到一万元及以上另需总经理审批。"},
    {"chunk_id": "d13_ch3", "doc_id": "d13", "title": "报销审批权限调整通知",
     "content": "差旅类报销在金额审批之外，仍需按《差旅与报销制度》在出差结束后三十天内提交。"},

    # d14 年终奖系数调整通知（与 d9_ch2 冲突）
    {"chunk_id": "d14_ch1", "doc_id": "d14", "title": "年终奖系数调整通知",
     "content": "2026 年度起，绩效为 A 的员工年终奖系数调整为 2.5。"},
    {"chunk_id": "d14_ch2", "doc_id": "d14", "title": "年终奖系数调整通知",
     "content": "B 档系数调整为 1.5，C 档维持 0.5，D 档仍不发放。"},
    {"chunk_id": "d14_ch3", "doc_id": "d14", "title": "年终奖系数调整通知",
     "content": "本调整仅适用于 2026 年度及以后年度的年终奖核算；2025 年度及以前的年终奖按原《年终奖制度》执行。"},

    # d15 加班与调休规定（缺口：加班工资倍数）
    {"chunk_id": "d15_ch1", "doc_id": "d15", "title": "加班与调休规定",
     "content": "加班需提前在 OA 系统提交申请，注明预计时长与工作内容，经直属主管审批后方可计入加班。"},
    {"chunk_id": "d15_ch2", "doc_id": "d15", "title": "加班与调休规定",
     "content": "工作日加班可按一比一折算调休，调休需在加班发生后六个月内使用，逾期作废。"},
    {"chunk_id": "d15_ch3", "doc_id": "d15", "title": "加班与调休规定",
     "content": "法定节假日加班按国家有关规定支付加班工资，计算基数为员工基本工资。"},

    # d16 员工福利概览（2025 年度，易混数字）
    {"chunk_id": "d16_ch1", "doc_id": "d16", "title": "员工福利概览（2025 年度）",
     "content": "2025 年度员工体检额度为每人八百元，可在公司合作的体检机构使用，超出部分自理。"},
    {"chunk_id": "d16_ch2", "doc_id": "d16", "title": "员工福利概览（2025 年度）",
     "content": "2025 年度节日礼金标准为每人一千元，于春节与中秋分两次发放。"},
    {"chunk_id": "d16_ch3", "doc_id": "d16", "title": "员工福利概览（2025 年度）",
     "content": "本概览所列标准仅适用于 2025 年度，年度调整以最新通知为准。"},

    # d17 培训与晋升补充（近义：转岗 / 借调 / 晋升）
    {"chunk_id": "d17_ch1", "doc_id": "d17", "title": "培训与晋升补充",
     "content": "内部转岗指员工在本岗位工作满六个月后申请转到目标部门，需通过目标部门面试并在人力资源部备案。"},
    {"chunk_id": "d17_ch2", "doc_id": "d17", "title": "培训与晋升补充",
     "content": "跨部门借调指因项目需要临时到其他部门工作，借调期一般不超过三个月，由双方部门负责人书面同意，无需面试。"},
    {"chunk_id": "d17_ch3", "doc_id": "d17", "title": "培训与晋升补充",
     "content": "岗位晋升与内部转岗不是同一事项：晋升指职级提升，需在当前岗位任职满一年且最近两次绩效为 A 或以上。"},

    # d18 考勤管理细则（缺口：全勤奖金额、迟到扣款金额）
    {"chunk_id": "d18_ch1", "doc_id": "d18", "title": "考勤管理细则",
     "content": "迟到指超过上午十点未打卡且未提前在 OA 报备。迟到超过三次影响当月全勤奖。"},
    {"chunk_id": "d18_ch2", "doc_id": "d18", "title": "考勤管理细则",
     "content": "全勤奖与月度绩效挂钩，具体发放标准由各部门在年度预算内自行确定。"},
    {"chunk_id": "d18_ch3", "doc_id": "d18", "title": "考勤管理细则",
     "content": "忘记打卡需在三个工作日内提交补卡申请，每月最多补卡三次。"},

    # d19 居家办公细则（近义：居家办公 vs 例行远程办公）
    {"chunk_id": "d19_ch1", "doc_id": "d19", "title": "居家办公细则",
     "content": "本细则所称居家办公，指因恶劣天气、家庭突发情况等原因全天在家办公，与《远程办公政策》中的例行远程办公不是同一事项。"},
    {"chunk_id": "d19_ch2", "doc_id": "d19", "title": "居家办公细则",
     "content": "居家办公需当日向直属主管报备，并在人力资源系统登记，不占用每周两天的远程办公额度。"},
    {"chunk_id": "d19_ch3", "doc_id": "d19", "title": "居家办公细则",
     "content": "居家办公期间考勤按正常出勤计算；连续居家办公超过三天需补充书面说明。"},

    # d20 员工福利调整通知（2026 年度，易混数字）
    {"chunk_id": "d20_ch1", "doc_id": "d20", "title": "员工福利调整通知（2026 年度）",
     "content": "自 2026 年度起，员工体检额度调整为每人一千二百元。"},
    {"chunk_id": "d20_ch2", "doc_id": "d20", "title": "员工福利调整通知（2026 年度）",
     "content": "2026 年度节日礼金标准调整为每人一千五百元。"},
    {"chunk_id": "d20_ch3", "doc_id": "d20", "title": "员工福利调整通知（2026 年度）",
     "content": "2025 年度已发放的福利不追溯调整；未使用的体检额度不跨年度累计。"},
]

AGENT_CORPUS = list(CORPUS) + INJECTED_CHUNKS

# 注入物清单：报告里要能说清"这 60 个 chunk 里有哪 27 个是我造的、造来干什么"。
INJECTIONS = [
    {"doc_id": "d12", "kind": "conflict", "targets": ["d2_ch1"],
     "note": "住宿上限 600 元 vs 500 元；不写废止，冲突不可判定"},
    {"doc_id": "d13", "kind": "conflict", "targets": ["d2_ch2"],
     "note": "审批门槛 3000 元 vs 1000 元"},
    {"doc_id": "d14", "kind": "conflict", "targets": ["d9_ch2"],
     "note": "A/B 系数 2.5/1.5 vs 2.0/1.2；带年度作用域"},
    {"doc_id": "d15", "kind": "gap", "targets": ["d7_ch3"],
     "note": "加班费只写按国家规定 + 计算基数，不给倍数"},
    {"doc_id": "d18", "kind": "gap", "targets": ["d7_ch2"],
     "note": "全勤奖金额、迟到扣款金额均写「由部门确定」"},
    {"doc_id": "d16", "kind": "confusable_number", "pair_with": "d20",
     "note": "体检 800 / 礼金 1000（2025 年度）"},
    {"doc_id": "d20", "kind": "confusable_number", "pair_with": "d16",
     "note": "体检 1200 / 礼金 1500（2026 年度）"},
    {"doc_id": "d17", "kind": "near_synonym", "group": ["内部转岗", "跨部门借调", "岗位晋升"],
     "note": "三个相近概念条件不同，用于澄清维度正反对照"},
    {"doc_id": "d19", "kind": "near_synonym", "group": ["居家办公", "远程办公"],
     "note": "居家办公当日报备，远程办公提前一天，额度不叠加"},
]


def corpus_hash() -> str:
    """语料指纹：报告里记它，才能证明两次运行用的是同一份语料。"""
    digest = hashlib.sha256()
    for chunk in sorted(AGENT_CORPUS, key=lambda item: item["chunk_id"]):
        digest.update(chunk["chunk_id"].encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(chunk["content"].encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def chunk_map() -> dict[str, dict]:
    return {chunk["chunk_id"]: chunk for chunk in AGENT_CORPUS}


def doc_inventory() -> list[dict]:
    """按文档聚合：doc_id / title / chunk 数 / 是否注入物。"""
    injected_docs = {item["doc_id"] for item in INJECTIONS}
    grouped: dict[str, dict] = {}
    for chunk in AGENT_CORPUS:
        doc = grouped.setdefault(
            chunk["doc_id"],
            {
                "doc_id": chunk["doc_id"],
                "title": chunk["title"],
                "chunks": 0,
                "injected": chunk["doc_id"] in injected_docs,
            },
        )
        doc["chunks"] += 1
    return [grouped[key] for key in sorted(grouped, key=lambda value: int(value[1:]))]
