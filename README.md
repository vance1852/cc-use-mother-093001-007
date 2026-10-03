# 管理作品商品化合作协作基础服务

本项目提供文化创意赛事与成果转化业务共享的服务端基础能力。基础层负责项目机构、
业务节点、操作者和结构化参考资料的登记；商品化合作层在此之上管理合作方资质、
作品设计版本、报价谈判、独家授权、会签与里程碑、只追加履约事实和可解释的收益结算。
两层共用角色权限、请求幂等、SQLite 事务与哈希串联审计。

## 目录

- src/creative_program_foundation/
  - 基础层：`domain.py`、`models.py`、`service.py`、`storage.py`、`audit.py`、`clock.py`、`api.py`、`acceptance.py`
  - 商品化层：
    - `merch_common.py`：事务、认证、幂等重放闸门与审计的共享基类
    - `merch_registry.py`：合作方资质/代表、团队/成员/签署授权、作品/设计版本会签、成本与分成口径
    - `merch_negotiation.py`：意向、要约、反要约、保留、会签、接受、撤回、终止状态机与独家占用
    - `merch_performance.py`：样品确认、部分交付、里程碑门禁、设计变更、违约整改、终止与结算/争议
    - `merch_money.py`：十进制金额、成本抵扣、保底、成员份额拆分的纯函数
    - `merch_service.py`：统一门面与按身份的最小必要信息投影
    - `merch_acceptance.py`：商品化离线验收
- tests/：基础规则、商品化状态机、权限视角、接口路由、恢复顺序与端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 核心生命周期规则

- 意向 → 要约 → 反要约（同线程、对方方向、前序要约未签署/未终局）→ 保留 →
  双方会签 → 接受成约；另有撤回、过期、终止，所有状态转移按时序校验。
- 独家地域 × 渠道在保留和接受两处都会与在效合同、在效保留做重叠检测，冲突即 409。
- 设计版本必须集齐团队要求的会签角色并完成全部前置事实，才能被要约接受并进入履约。
- 样品/交付/审批里程碑按依赖顺序解锁，交付只追加并累计，重复请求回执不会重复记账。
- 设计变更、违约整改、结算争议均以新事实或纠正结算追加，原合同条款与口径永不被改写。
- 每笔结算冻结成本口径与成员份额口径的编号和哈希，可经 `/settlements/{id}/explain` 解释。
- 团队、合作方、运营看到最小必要信息：合作方看不到团队内部成员份额明细。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m creative_program_foundation.acceptance
    PYTHONPATH=src python3 -m creative_program_foundation.merch_acceptance

验收命令在临时 SQLite 数据库中走完整链路，成功时输出一行 status 为 ok 的 JSON
并以退出码 0 结束。商品化验收覆盖独家冲突拦截、重复回调幂等、结算口径解释、
事实追加与终止后的权利释放。

## HTTP 服务

    PYTHONPATH=src python3 -m creative_program_foundation.api --database creative_program.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者并要求 request_id
做幂等键，服务重启后 SQLite 中的业务状态、待确认要约顺序和审计历史继续保留。
主要写入端点包括：

- 登记：/partners、/partner-qualifications、/partner-representatives、/teams、
  /team-members、/signing-grants、/works、/design-versions、
  /design-versions/countersign、/cost-sheets、/share-sheets
- 谈判：/intentions、/offers、/offers/reserve、/offers/sign、/offers/accept、
  /offers/withdraw、/offers/expire
- 履约：/samples、/deliveries、/change-orders、/breaches、
  /breaches/remediation、/contracts/terminate、/settlements、
  /settlements/disputes/resolve
- 查询：/offers、/contracts、/works/{id}/occupancy、
  /design-versions/{id}/readiness、/contracts/{id}/facts、
  /contracts/{id}/deliveries、/settlements/{id}、/settlements/{id}/explain
