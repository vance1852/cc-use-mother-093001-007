# 管理作品商品化合作协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力，负责项目机构、业务节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。各领域模块可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上，`commercialization` 模块实现了作品商品化合作管理服务：把合作方资质、作品与设计版本、报价要约、独家地域与渠道、样品确认、最低承诺、成本口径、收益分配、交付里程碑和签署权限组织成可追踪的生命周期，避免用聊天记录推进合作时接受冲突条件或把后续改稿错误计入已签合同。

## 目录

- src/creative_program_foundation/：领域模型、SQLite 存储、权限服务、审计链、商品化合作管理服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、合作生命周期和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记项目机构、操作者、业务节点和参考资料，核对幂等回执与审计链，并演练一条从合作意向到结算争议的完整商品化合作链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## 合作生命周期

- 协商：登记意向（open_negotiation）后，双方交替要约（make_offer）与反要约（counter_offer，原要约随即被取代）；团队可保留（reserve_offer）/解除保留（release_offer）冻结协商窗口；要约方可在接受前撤回（withdraw_offer）；对方接受（accept_offer）最新未决要约后生成待会签合同。所有迁移按顺序校验，保留中的要约不能接受或反要约。
- 独家冲突：接受要约时校验其独家地域×渠道与同一作品的有效合同（待会签、履约中）不重叠；合同终止（terminate_contract）后权利即时释放。
- 履约门禁：只有会签人全部签署且 gate 里程碑（如样品确认）按依赖顺序完成后，合同才能激活（activate_contract）进入履约；部分交付只能登记在履约中的合同上。
- 只追加事实：设计变更、部分交付、成本口径与成员份额修订、违约与整改、结算争议都作为新事实追加，原合同条款与历史版本不被篡改。
- 结算：按结算时最新的成本口径版本与成员份额版本计算分录；同一合同同一周期只能结算一次，重复回调通过 request_id 幂等返回原回执，不会重复签约或重复入账。
- 审计：rights_at 解释某时点作品哪些地域×渠道权利仍被哪些合同占用；explain_settlement 解释某笔分成采用了哪一版成本和成员份额；全部状态变化写入哈希串联审计日志，服务重启后待确认要约与里程碑顺序不变。

## 角色与最小必要视图

- admin/operator（团队）：登记与履约动作；reviewer/auditor：只读与审计查询；
- partner（合作方联系人）：只能发起和查看自己合作方的协商、要约、合同与结算，视图中隐藏团队内部成本明细、他人份额和会签人名单，仅看到自己的份额行。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

基础接口：POST /organizations、/actors、/sites、/domain-records，GET /domain-records、/audit-events。

商品化接口（POST 均需 request_id 幂等键）：

- 登记：POST /partners、/partner-members、/works、/design-versions；
- 协商：POST /negotiations、/offers、/offers/{id}/counter|reserve|release|withdraw|accept，GET /negotiations/{id}、/pending-offers；
- 合同：POST /contracts/{id}/sign|activate|terminate、/contracts/{id}/milestones/{key}/complete、/contracts/{id}/deliveries|design-changes|cost-revisions|share-revisions|breaches|rectifications，GET /contracts/{id}、/contracts/{id}/timeline；
- 结算：POST /contracts/{id}/settlements、/settlements/{id}/disputes，GET /settlements/{id}、/settlements/{id}/explanation；
- 审计：GET /works/{id}/rights?at=<ISO-8601>。
